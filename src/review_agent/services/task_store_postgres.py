from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from review_agent.db.codec import (
    acceptance_to_json,
    call_hash,
    event_from_row,
    iso,
    message_from_parts,
    parse_dt,
    public_event_payload,
    run_from_row,
    tool_call_from_json,
    tool_call_to_json,
    tool_result_from_json,
    tool_result_to_json,
    utcnow,
)
from review_agent.db.engine import create_db_engine, make_session_factory, session_scope
from review_agent.db.models import (
    ApprovalRow,
    ArtifactRow,
    EventRow,
    MessageRow,
    RunRow,
    SessionRow,
    ToolExecutionRow,
)
from review_agent.harness.models import (
    ACTIVE_RUN_STATUSES,
    AgentEvent,
    Message,
    ReplayCategory,
    RunStatus,
    ToolCall,
    ToolResult,
    TaskMode,
)
from review_agent.harness.task_store import (
    AcceptanceSpec,
    normalize_task_request,
    same_run_request,
    ApprovalRecord,
    ArtifactRecord,
    RunRecord,
    SessionRecord,
    ToolExecutionRecord,
)

from review_agent.harness.review_target import ReviewTarget


class PostgresTaskStore:
    """PostgreSQL TaskStore. Official LangGraph saver tables are not managed here."""

    def __init__(self, engine: Engine | None = None, *, url: str | None = None) -> None:
        if engine is None:
            if not url:
                raise ValueError("PostgresTaskStore requires engine or url")
            engine = create_db_engine(url)
        self.engine = engine
        self._factory = make_session_factory(engine)

    def create_session(self, workspace_id: str, *, session_id: str | None = None) -> SessionRecord:
        sid = session_id or str(uuid.uuid4())
        now = utcnow()
        with session_scope(self._factory) as db:
            if db.get(SessionRow, sid) is not None:
                raise ValueError(f"session already exists: {sid}")
            row = SessionRow(session_id=sid, workspace_id=workspace_id, created_at=now)
            db.add(row)
        return SessionRecord(session_id=sid, workspace_id=workspace_id, created_at=iso(now))

    def get_session(self, session_id: str) -> SessionRecord | None:
        with session_scope(self._factory) as db:
            row = db.get(SessionRow, session_id)
            if row is None:
                return None
            return _session_record(row)

    def list_sessions(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[SessionRecord], str | None]:
        limit = max(1, min(limit, 100))
        with session_scope(self._factory) as db:
            stmt = select(SessionRow).order_by(SessionRow.created_at.asc(), SessionRow.session_id.asc())
            rows = list(db.scalars(stmt))
        items = [_session_record(row) for row in rows]
        if cursor:
            created_at, session_id = _split_cursor(cursor)
            items = [item for item in items if (item.created_at, item.session_id) > (created_at, session_id)]
        page = items[:limit]
        next_cursor = None
        if len(items) > len(page):
            last = page[-1]
            next_cursor = f"{last.created_at}|{last.session_id}"
        return page, next_cursor

    def create_run(
        self,
        session_id: str,
        requirement: str,
        *,
        run_id: str | None = None,
        profile_id: str = "deepseek",
        acceptance: AcceptanceSpec | None = None,
        idempotency_key: str | None = None,
        reviewer: str = "default",
        task_mode: TaskMode = "develop",
        review_target: ReviewTarget | None = None,
    ) -> RunRecord:
        return self._insert_run(
            session_id,
            requirement,
            run_id=run_id,
            profile_id=profile_id,
            acceptance=acceptance,
            idempotency_key=idempotency_key,
            dispatch_pending=False,
            reviewer=reviewer,
            task_mode=task_mode,
            review_target=review_target,
        )

    def create_run_bundle(
        self,
        session_id: str,
        requirement: str,
        *,
        idempotency_key: str,
        profile_id: str = "deepseek",
        acceptance: AcceptanceSpec | None = None,
        message_id: str | None = None,
        workspace_snapshot: dict[str, Any] | None = None,
        request_summary: dict[str, Any] | None = None,
        run_id: str | None = None,
        reviewer: str = "default",
        task_mode: TaskMode = "develop",
        review_target: ReviewTarget | None = None,
    ) -> RunRecord:
        from review_agent.harness.review_state import initial_review_state

        now = utcnow()
        rid = run_id or str(uuid.uuid4())
        mid = message_id or str(uuid.uuid4())
        spec, review_target = normalize_task_request(task_mode, review_target, acceptance, reviewer)
        with session_scope(self._factory) as db:
            session_row = db.execute(
                select(SessionRow).where(SessionRow.session_id == session_id).with_for_update()
            ).scalar_one_or_none()
            if session_row is None:
                raise KeyError(f"unknown session: {session_id}")
            existing = self._locked_idempotent_run(db, session_id, idempotency_key)
            if existing is not None:
                if not same_run_request(_run_record(existing), requirement, profile_id, spec, reviewer, task_mode, review_target):
                    raise ValueError("idempotency key conflict")
                return _run_record(existing)
            if self._active_run_row(db, session_id) is not None:
                raise RuntimeError("session already has an active run")
            run = RunRow(
                run_id=rid,
                session_id=session_id,
                status=RunStatus.QUEUED.value,
                requirement=requirement,
                profile_id=profile_id,
                acceptance=acceptance_to_json(spec),
                idempotency_key=idempotency_key,
                cancel_requested=False,
                dispatch_pending=True,
                workspace_snapshot=workspace_snapshot,
                request_summary=request_summary,
                review=initial_review_state("off" if task_mode == "plan" else reviewer),
                task_mode=task_mode,
                review_target=review_target.model_dump(mode="json") if review_target else None,
                created_at=now,
                updated_at=now,
            )
            db.add(run)
            try:
                db.flush()
                db.add(
                    MessageRow(
                        message_id=mid,
                        session_id=session_id,
                        run_id=rid,
                        role="user",
                        content=requirement,
                        pending=False,
                        created_at=now,
                    )
                )
                db.add(
                    EventRow(
                        run_id=rid,
                        seq=1,
                        timestamp=now,
                        type="run.created",
                        message=f"Run created for session {session_id}",
                        payload={
                            "requirement": requirement,
                            **({"request_summary": request_summary} if request_summary else {}),
                        },
                        call_id=None,
                    )
                )
                db.flush()
            except IntegrityError as exc:
                return self._resolve_create_conflict(db, session_id, idempotency_key, requirement, exc,
                    profile_id=profile_id, spec=spec, reviewer=reviewer, task_mode=task_mode, review_target=review_target)
            db.refresh(run)
            return _run_record(run)

    def get_run(self, run_id: str) -> RunRecord | None:
        with session_scope(self._factory) as db:
            row = db.get(RunRow, run_id)
            return _run_record(row) if row is not None else None

    def list_runs(self, session_id: str) -> list[RunRecord]:
        with session_scope(self._factory) as db:
            rows = list(
                db.scalars(
                    select(RunRow)
                    .where(RunRow.session_id == session_id)
                    .order_by(RunRow.created_at.asc(), RunRow.run_id.asc())
                )
            )
            return [_run_record(row) for row in rows]

    def active_run(self, session_id: str) -> RunRecord | None:
        with session_scope(self._factory) as db:
            row = self._active_run_row(db, session_id)
            return _run_record(row) if row is not None else None

    def update_run(
        self,
        run_id: str,
        *,
        status: RunStatus | None = None,
        cancel_requested: bool | None = None,
        workspace_revision: str | None = None,
        verification: dict[str, Any] | None = None,
        dispatch_pending: bool | None = None,
        usage: dict[str, Any] | None = None,
        budget: dict[str, Any] | None = None,
        owner_id: str | None = None,
        lease_until: str | None = None,
        heartbeat_at: str | None = None,
        attention: dict[str, Any] | None = None,
        review: dict[str, Any] | None = None,
        clear_owner: bool = False,
        workspace_snapshot: dict[str, Any] | None = None,
    ) -> RunRecord:
        with session_scope(self._factory) as db:
            row = db.get(RunRow, run_id)
            if row is None:
                raise KeyError(f"unknown run: {run_id}")
            if workspace_snapshot is not None:
                row.workspace_snapshot = dict(workspace_snapshot)
            if status is not None:
                row.status = status.value if isinstance(status, RunStatus) else str(status)
            if cancel_requested is not None:
                row.cancel_requested = cancel_requested
            if workspace_revision is not None:
                row.workspace_revision = workspace_revision
            if verification is not None:
                row.verification = verification
            if dispatch_pending is not None:
                row.dispatch_pending = dispatch_pending
            if usage is not None:
                row.usage = usage
            if budget is not None:
                row.budget = budget
            if owner_id is not None:
                row.owner_id = owner_id
            if lease_until is not None:
                row.lease_until = parse_dt(lease_until)
            if heartbeat_at is not None:
                row.heartbeat_at = parse_dt(heartbeat_at)
            if attention is not None:
                row.attention = attention
            if review is not None:
                row.review = review
            if clear_owner:
                row.owner_id = None
                row.lease_until = None
                row.heartbeat_at = None
            row.updated_at = utcnow()
            db.flush()
            db.refresh(row)
            return _run_record(row)

    def list_dispatch_pending(self) -> list[RunRecord]:
        with session_scope(self._factory) as db:
            rows = list(db.scalars(select(RunRow).where(RunRow.dispatch_pending.is_(True))))
            return [_run_record(row) for row in rows]

    def claim_run(
        self,
        run_id: str,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> RunRecord | None:
        from review_agent.services.lease import lease_expired, lease_until_iso, run_is_claimable

        now = utcnow()
        with session_scope(self._factory) as db:
            row = db.execute(select(RunRow).where(RunRow.run_id == run_id).with_for_update()).scalar_one_or_none()
            if row is None:
                return None
            run = _run_record(row)
            approvals = [
                _approval_record(item)
                for item in db.scalars(select(ApprovalRow).where(ApprovalRow.run_id == run_id))
            ]
            if not run_is_claimable(run, approvals=approvals):
                return None
            if run.owner_id and run.owner_id != owner_id and not lease_expired(run.lease_until, now=now):
                return None
            row.owner_id = owner_id
            row.lease_until = parse_dt(lease_until_iso(lease_seconds, now=now))
            row.heartbeat_at = now
            row.status = RunStatus.RUNNING.value
            row.dispatch_pending = False
            row.updated_at = now
            db.flush()
            db.refresh(row)
            return _run_record(row)

    def heartbeat_run(self, run_id: str, owner_id: str, *, lease_seconds: int) -> RunRecord | None:
        from review_agent.services.lease import lease_until_iso

        now = utcnow()
        with session_scope(self._factory) as db:
            row = db.get(RunRow, run_id)
            if row is None or row.owner_id != owner_id:
                return None
            row.lease_until = parse_dt(lease_until_iso(lease_seconds, now=now))
            row.heartbeat_at = now
            row.updated_at = now
            db.flush()
            db.refresh(row)
            return _run_record(row)

    def release_run(self, run_id: str, owner_id: str) -> RunRecord | None:
        with session_scope(self._factory) as db:
            row = db.get(RunRow, run_id)
            if row is None or row.owner_id != owner_id:
                return None
            row.owner_id = None
            row.lease_until = None
            row.heartbeat_at = None
            row.updated_at = utcnow()
            db.flush()
            db.refresh(row)
            return _run_record(row)

    def get_tool_execution(self, call_id: str) -> ToolExecutionRecord | None:
        with session_scope(self._factory) as db:
            row = db.get(ToolExecutionRow, call_id)
            return _tool_record(row) if row is not None else None

    def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        with session_scope(self._factory) as db:
            rows = list(db.scalars(select(ToolExecutionRow).where(ToolExecutionRow.run_id == run_id)))
            return [_tool_record(row) for row in rows]

    def append_message(self, message: Message) -> Message:
        mid = message.message_id or str(uuid.uuid4())
        now = utcnow()
        with session_scope(self._factory) as db:
            existing = db.get(MessageRow, mid)
            if existing is not None:
                return _message_record(existing)
            db.add(
                MessageRow(
                    message_id=mid,
                    session_id=message.session_id,
                    run_id=message.run_id or None,
                    role=message.role,
                    content=message.content,
                    tool_calls=[tool_call_to_json(item) for item in message.tool_calls]
                    if message.tool_calls
                    else None,
                    provider_call_id=message.provider_call_id,
                    pending=False,
                    created_at=now,
                )
            )
        return Message(
            role=message.role,
            content=message.content,
            message_id=mid,
            session_id=message.session_id,
            run_id=message.run_id,
            tool_calls=message.tool_calls,
            provider_call_id=message.provider_call_id,
        )

    def list_messages(
        self,
        session_id: str,
        *,
        before_run_id: str | None = None,
        include_run_id: str | None = None,
    ) -> list[Message]:
        with session_scope(self._factory) as db:
            runs = {
                row.run_id: row
                for row in db.scalars(select(RunRow).where(RunRow.session_id == session_id))
            }
            allowed: set[str] | None = None
            if before_run_id is not None:
                cutoff = runs[before_run_id].created_at
                allowed = {
                    rid for rid, row in runs.items() if row.created_at < cutoff or rid == before_run_id
                }
            if include_run_id is not None:
                allowed = (allowed or set(runs)) | {include_run_id}
            rows = list(
                db.scalars(
                    select(MessageRow)
                    .where(MessageRow.session_id == session_id)
                    .order_by(MessageRow.created_at.asc(), MessageRow.message_id.asc())
                )
            )
            result: list[Message] = []
            for row in rows:
                if allowed is not None and row.run_id and row.run_id not in allowed:
                    continue
                result.append(_message_record(row))
            return result

    def enqueue_pending(self, run_id: str, message: Message) -> Message:
        mid = message.message_id or str(uuid.uuid4())
        now = utcnow()
        with session_scope(self._factory) as db:
            if db.get(RunRow, run_id) is None:
                raise KeyError(f"unknown run: {run_id}")
            existing = db.get(MessageRow, mid)
            if existing is not None:
                existing.pending = True
                return _message_record(existing)
            row = MessageRow(
                message_id=mid,
                session_id=message.session_id,
                run_id=run_id,
                role=message.role,
                content=message.content,
                tool_calls=[tool_call_to_json(item) for item in message.tool_calls]
                if message.tool_calls
                else None,
                provider_call_id=message.provider_call_id,
                pending=True,
                created_at=now,
            )
            db.add(row)
            db.flush()
            return _message_record(row)

    def drain_pending(self, run_id: str) -> list[Message]:
        with session_scope(self._factory) as db:
            rows = list(
                db.scalars(
                    select(MessageRow)
                    .where(MessageRow.run_id == run_id, MessageRow.pending.is_(True))
                    .order_by(MessageRow.created_at.asc())
                )
            )
            messages = [_message_record(row) for row in rows]
            for row in rows:
                row.pending = False
            return messages

    def append_event(self, event: AgentEvent) -> AgentEvent:
        payload, call_id = public_event_payload(event)
        now = utcnow()
        with session_scope(self._factory) as db:
            run = db.execute(select(RunRow).where(RunRow.run_id == event.run_id).with_for_update()).scalar_one_or_none()
            if run is None:
                raise KeyError(f"unknown run for event: {event.run_id}")
            last = db.execute(
                select(EventRow.seq).where(EventRow.run_id == event.run_id).order_by(EventRow.seq.desc()).limit(1)
            ).scalar_one_or_none()
            seq = int(last or 0) + 1
            db.add(
                EventRow(
                    run_id=event.run_id,
                    seq=seq,
                    timestamp=now,
                    type=event.type,
                    message=event.message,
                    payload=payload,
                    call_id=call_id,
                )
            )
        return AgentEvent(
            type=event.type,
            message=event.message,
            run_id=event.run_id,
            seq=seq,
            payload=payload,
        )

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[AgentEvent]:
        with session_scope(self._factory) as db:
            rows = list(
                db.scalars(
                    select(EventRow)
                    .where(EventRow.run_id == run_id, EventRow.seq > after_seq)
                    .order_by(EventRow.seq.asc())
                )
            )
            return [
                event_from_row(
                    event_type=row.type,
                    message=row.message,
                    run_id=row.run_id,
                    seq=row.seq,
                    payload=row.payload,
                )
                for row in rows
            ]

    def record_tool_execution(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call: ToolCall,
        result: ToolResult | None = None,
        replay_category: str | ReplayCategory | None = None,
    ) -> ToolExecutionRecord:
        call_id = tool_call.call_id or str(uuid.uuid4())
        now = utcnow()
        category = replay_category.value if isinstance(replay_category, ReplayCategory) else replay_category
        with session_scope(self._factory) as db:
            existing = db.get(ToolExecutionRow, call_id)
            if existing is not None and result is None:
                return _tool_record(existing)
            if existing is not None:
                existing.result = tool_result_to_json(result)
                if category and not existing.replay_category:
                    existing.replay_category = category
                db.flush()
                return _tool_record(existing)
            normalized = ToolCall(
                name=tool_call.name,
                arguments=dict(tool_call.arguments or {}),
                call_id=call_id,
                provider_call_id=tool_call.provider_call_id,
            )
            row = ToolExecutionRow(
                call_id=call_id,
                run_id=run_id,
                session_id=session_id,
                tool_call=tool_call_to_json(normalized),
                result=tool_result_to_json(result),
                call_hash=call_hash(normalized),
                created_at=now,
                replay_category=category,
            )
            db.add(row)
            db.flush()
            return _tool_record(row)

    def complete_tool_execution(
        self,
        call_id: str,
        result: ToolResult | None,
        *,
        execution_meta: dict[str, Any] | None = None,
    ) -> ToolExecutionRecord:
        with session_scope(self._factory) as db:
            row = db.get(ToolExecutionRow, call_id)
            if row is None:
                raise KeyError(f"unknown tool execution: {call_id}")
            row.result = tool_result_to_json(result)
            if execution_meta:
                row.execution_id = execution_meta.get("execution_id") or row.execution_id
                row.argv = execution_meta.get("argv") or row.argv
                row.started_at = execution_meta.get("started_at") or row.started_at
                row.finished_at = execution_meta.get("finished_at") or row.finished_at
                row.pid = execution_meta.get("pid", row.pid)
                row.pgid = execution_meta.get("pgid", row.pgid)
                row.container_name = execution_meta.get("container_name") or row.container_name
                row.container_id = execution_meta.get("container_id") or row.container_id
                row.workspace_revision = execution_meta.get("workspace_revision") or row.workspace_revision
                row.stdout_artifact = execution_meta.get("stdout_artifact") or row.stdout_artifact
                row.stderr_artifact = execution_meta.get("stderr_artifact") or row.stderr_artifact
                row.patch_hash = execution_meta.get("patch_hash") or row.patch_hash
                row.execution_status = execution_meta.get("status") or row.execution_status
            db.flush()
            return _tool_record(row)

    def register_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        now = utcnow()
        with session_scope(self._factory) as db:
            existing = db.execute(
                select(ArtifactRow).where(
                    ArtifactRow.run_id == record.run_id,
                    ArtifactRow.storage_path == record.storage_path,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _artifact_record(existing)
            row = ArtifactRow(
                artifact_id=record.artifact_id,
                run_id=record.run_id,
                kind=record.kind,
                summary=record.summary,
                size_bytes=record.size_bytes,
                content_hash=record.content_hash,
                storage_path=record.storage_path,
                created_at=parse_dt(record.created_at) if record.created_at else now,
            )
            db.add(row)
            db.flush()
            return _artifact_record(row)

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with session_scope(self._factory) as db:
            rows = list(db.scalars(select(ArtifactRow).where(ArtifactRow.run_id == run_id)))
            return [_artifact_record(row) for row in rows]

    def get_artifact(self, run_id: str, artifact_id: str) -> ArtifactRecord | None:
        with session_scope(self._factory) as db:
            row = db.get(ArtifactRow, artifact_id)
            if row is None or row.run_id != run_id:
                return None
            return _artifact_record(row)

    def create_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        now = utcnow()
        with session_scope(self._factory) as db:
            existing = db.get(ApprovalRow, record.approval_id)
            if existing is not None:
                return _approval_record(existing)
            row = ApprovalRow(
                approval_id=record.approval_id,
                run_id=record.run_id,
                call_id=record.call_id,
                intent=dict(record.intent),
                param_summary=record.param_summary,
                workspace_revision=record.workspace_revision,
                patch_hash=record.patch_hash,
                decision=record.decision,
                created_at=parse_dt(record.created_at) if record.created_at else now,
                decided_at=parse_dt(record.decided_at) if record.decided_at else None,
            )
            db.add(row)
            db.flush()
            return _approval_record(row)

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with session_scope(self._factory) as db:
            row = db.get(ApprovalRow, approval_id)
            return _approval_record(row) if row is not None else None

    def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        with session_scope(self._factory) as db:
            rows = list(db.scalars(select(ApprovalRow).where(ApprovalRow.run_id == run_id)))
            return [_approval_record(row) for row in rows]

    def set_approval_decision(self, approval_id: str, decision: str) -> ApprovalRecord:
        with session_scope(self._factory) as db:
            row = db.get(ApprovalRow, approval_id)
            if row is None:
                raise KeyError(f"unknown approval: {approval_id}")
            if row.decision is not None and row.decision != decision:
                raise ValueError("approval decision conflict")
            row.decision = decision
            row.decided_at = utcnow()
            db.flush()
            return _approval_record(row)

    def _insert_run(
        self,
        session_id: str,
        requirement: str,
        *,
        run_id: str | None,
        profile_id: str,
        acceptance: AcceptanceSpec | None,
        idempotency_key: str | None,
        dispatch_pending: bool,
        reviewer: str = "default",
        task_mode: TaskMode = "develop",
        review_target: ReviewTarget | None = None,
    ) -> RunRecord:
        from review_agent.harness.review_state import initial_review_state

        now = utcnow()
        rid = run_id or str(uuid.uuid4())
        spec, review_target = normalize_task_request(task_mode, review_target, acceptance, reviewer)
        with session_scope(self._factory) as db:
            session_row = db.execute(
                select(SessionRow).where(SessionRow.session_id == session_id).with_for_update()
            ).scalar_one_or_none()
            if session_row is None:
                raise KeyError(f"unknown session: {session_id}")
            if idempotency_key:
                existing = self._locked_idempotent_run(db, session_id, idempotency_key)
                if existing is not None:
                    if not same_run_request(_run_record(existing), requirement, profile_id, spec, reviewer, task_mode, review_target):
                        raise ValueError("idempotency key conflict")
                    return _run_record(existing)
            if self._active_run_row(db, session_id) is not None:
                raise RuntimeError("session already has an active run")
            row = RunRow(
                run_id=rid,
                session_id=session_id,
                status=RunStatus.QUEUED.value,
                requirement=requirement,
                profile_id=profile_id,
                acceptance=acceptance_to_json(spec),
                idempotency_key=idempotency_key,
                cancel_requested=False,
                dispatch_pending=dispatch_pending,
                review=initial_review_state("off" if task_mode == "plan" else reviewer),
                task_mode=task_mode,
                review_target=review_target.model_dump(mode="json") if review_target else None,
                created_at=now,
                updated_at=now,
            )
            db.add(row)
            try:
                db.flush()
            except IntegrityError as exc:
                return self._resolve_create_conflict(db, session_id, idempotency_key, requirement, exc,
                    profile_id=profile_id, spec=spec, reviewer=reviewer, task_mode=task_mode, review_target=review_target)
            db.refresh(row)
            return _run_record(row)

    def _locked_idempotent_run(self, db: Session, session_id: str, idempotency_key: str) -> RunRow | None:
        return db.execute(
            select(RunRow)
            .where(RunRow.session_id == session_id, RunRow.idempotency_key == idempotency_key)
            .with_for_update()
        ).scalar_one_or_none()

    def _active_run_row(self, db: Session, session_id: str) -> RunRow | None:
        active = [status.value for status in ACTIVE_RUN_STATUSES]
        return db.execute(
            select(RunRow).where(RunRow.session_id == session_id, RunRow.status.in_(active))
        ).scalars().first()

    def _resolve_create_conflict(
        self,
        db: Session,
        session_id: str,
        idempotency_key: str | None,
        requirement: str,
        exc: IntegrityError,
        *, profile_id: str, spec: AcceptanceSpec, reviewer: str,
        task_mode: TaskMode, review_target: ReviewTarget | None,
    ) -> RunRecord:
        db.rollback()
        if idempotency_key:
            existing = db.execute(
                select(RunRow).where(
                    RunRow.session_id == session_id, RunRow.idempotency_key == idempotency_key
                )
            ).scalar_one_or_none()
            if existing is not None:
                if not same_run_request(_run_record(existing), requirement, profile_id, spec, reviewer, task_mode, review_target):
                    raise ValueError("idempotency key conflict") from exc
                return _run_record(existing)
        raise RuntimeError("session already has an active run") from exc


def _session_record(row: SessionRow) -> SessionRecord:
    return SessionRecord(
        session_id=row.session_id,
        workspace_id=row.workspace_id,
        created_at=iso(row.created_at),
    )


def _run_record(row: RunRow) -> RunRecord:
    return run_from_row(
        run_id=row.run_id,
        session_id=row.session_id,
        status=row.status,
        requirement=row.requirement,
        profile_id=row.profile_id,
        task_mode=row.task_mode,
        review_target=row.review_target,
        acceptance=row.acceptance,
        idempotency_key=row.idempotency_key,
        cancel_requested=row.cancel_requested,
        workspace_revision=row.workspace_revision,
        verification=row.verification,
        dispatch_pending=row.dispatch_pending,
        budget=row.budget,
        usage=row.usage,
        workspace_snapshot=row.workspace_snapshot,
        created_at=iso(row.created_at),
        updated_at=iso(row.updated_at),
        owner_id=row.owner_id,
        lease_until=iso(row.lease_until) if row.lease_until else None,
        heartbeat_at=iso(row.heartbeat_at) if row.heartbeat_at else None,
        attention=row.attention,
        review=row.review,
    )


def _message_record(row: MessageRow) -> Message:
    return message_from_parts(
        role=row.role,
        content=row.content,
        message_id=row.message_id,
        session_id=row.session_id,
        run_id=row.run_id,
        tool_calls=row.tool_calls,
        provider_call_id=row.provider_call_id,
    )


def _tool_record(row: ToolExecutionRow) -> ToolExecutionRecord:
    return ToolExecutionRecord(
        call_id=row.call_id,
        run_id=row.run_id,
        session_id=row.session_id,
        tool_call=tool_call_from_json(row.tool_call),
        result=tool_result_from_json(row.result),
        created_at=iso(row.created_at),
        execution_id=row.execution_id,
        argv=list(row.argv) if row.argv else None,
        started_at=row.started_at,
        finished_at=row.finished_at,
        pid=row.pid,
        pgid=row.pgid,
        container_name=row.container_name,
        container_id=row.container_id,
        workspace_revision=row.workspace_revision,
        stdout_artifact=row.stdout_artifact,
        stderr_artifact=row.stderr_artifact,
        patch_hash=row.patch_hash,
        execution_status=row.execution_status,
        replay_category=row.replay_category,
    )


def _artifact_record(row: ArtifactRow) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=row.artifact_id,
        run_id=row.run_id,
        kind=row.kind,
        summary=row.summary,
        size_bytes=int(row.size_bytes),
        content_hash=row.content_hash,
        storage_path=row.storage_path,
        created_at=iso(row.created_at),
    )


def _approval_record(row: ApprovalRow) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=row.approval_id,
        run_id=row.run_id,
        intent=dict(row.intent or {}),
        param_summary=row.param_summary,
        workspace_revision=row.workspace_revision,
        patch_hash=row.patch_hash,
        call_id=row.call_id,
        decision=row.decision,
        created_at=iso(row.created_at),
        decided_at=iso(row.decided_at) if row.decided_at else None,
    )


def _split_cursor(cursor: str) -> tuple[str, str]:
    created_at, _, ident = cursor.partition("|")
    if not ident:
        raise ValueError("invalid cursor")
    return created_at, ident
