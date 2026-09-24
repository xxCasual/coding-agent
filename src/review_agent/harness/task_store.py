from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

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

from review_agent.harness.review_target import ReviewTarget


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionRecord:
    session_id: str
    workspace_id: str
    created_at: str = field(default_factory=_now)


@dataclass
class AcceptanceSpec:
    """Frozen at run start; Agent cannot cancel required checks by editing project files."""

    mode: str = "unverified"  # unverified | not_applicable | commands
    checks: list[list[str]] = field(default_factory=list)
    description: str = ""


@dataclass
class RunRecord:
    run_id: str
    session_id: str
    status: RunStatus
    requirement: str
    profile_id: str = "deepseek"
    acceptance: AcceptanceSpec = field(default_factory=AcceptanceSpec)
    idempotency_key: str | None = None
    cancel_requested: bool = False
    workspace_revision: str | None = None
    verification: dict[str, Any] | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    dispatch_pending: bool = False
    budget: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    workspace_snapshot: dict[str, Any] | None = None
    owner_id: str | None = None
    lease_until: str | None = None
    heartbeat_at: str | None = None
    attention: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    task_mode: TaskMode = "develop"
    review_target: ReviewTarget | None = None


@dataclass
class ToolExecutionRecord:
    call_id: str
    run_id: str
    session_id: str
    tool_call: ToolCall
    result: ToolResult | None = None
    created_at: str = field(default_factory=_now)
    # M04 recovery observables for M08 (optional until command tools run).
    execution_id: str | None = None
    argv: list[str] | None = None
    started_at: str | None = None
    finished_at: str | None = None
    pid: int | None = None
    pgid: int | None = None
    container_name: str | None = None
    container_id: str | None = None
    workspace_revision: str | None = None
    stdout_artifact: str | None = None
    stderr_artifact: str | None = None
    patch_hash: str | None = None
    execution_status: str | None = None
    replay_category: str | None = None


@dataclass
class ArtifactRecord:
    artifact_id: str
    run_id: str
    kind: str
    summary: str
    size_bytes: int
    content_hash: str
    storage_path: str
    created_at: str = field(default_factory=_now)


@dataclass
class ApprovalRecord:
    approval_id: str
    run_id: str
    intent: dict[str, Any]
    param_summary: str = ""
    workspace_revision: str | None = None
    patch_hash: str | None = None
    call_id: str | None = None
    decision: str | None = None
    created_at: str = field(default_factory=_now)
    decided_at: str | None = None


class TaskStore(Protocol):
    def create_session(self, workspace_id: str, *, session_id: str | None = None) -> SessionRecord:
        ...

    def get_session(self, session_id: str) -> SessionRecord | None:
        ...

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
        ...

    def get_run(self, run_id: str) -> RunRecord | None:
        ...

    def list_runs(self, session_id: str) -> list[RunRecord]:
        ...

    def active_run(self, session_id: str) -> RunRecord | None:
        ...

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
        ...

    def list_dispatch_pending(self) -> list[RunRecord]:
        ...

    def claim_run(
        self,
        run_id: str,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> RunRecord | None:
        ...

    def heartbeat_run(self, run_id: str, owner_id: str, *, lease_seconds: int) -> RunRecord | None:
        ...

    def release_run(self, run_id: str, owner_id: str) -> RunRecord | None:
        ...

    def get_tool_execution(self, call_id: str) -> ToolExecutionRecord | None:
        ...

    def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        ...

    def append_message(self, message: Message) -> Message:
        ...

    def list_messages(
        self,
        session_id: str,
        *,
        before_run_id: str | None = None,
        include_run_id: str | None = None,
    ) -> list[Message]:
        ...

    def enqueue_pending(self, run_id: str, message: Message) -> Message:
        ...

    def drain_pending(self, run_id: str) -> list[Message]:
        ...

    def append_event(self, event: AgentEvent) -> AgentEvent:
        ...

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[AgentEvent]:
        ...

    def record_tool_execution(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call: ToolCall,
        result: ToolResult | None = None,
        replay_category: str | ReplayCategory | None = None,
    ) -> ToolExecutionRecord:
        ...

    def complete_tool_execution(
        self,
        call_id: str,
        result: ToolResult | None,
        *,
        execution_meta: dict[str, Any] | None = None,
    ) -> ToolExecutionRecord:
        ...

    def create_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        ...

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        ...

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
        ...

    def list_sessions(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[SessionRecord], str | None]:
        ...

    def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        ...

    def set_approval_decision(self, approval_id: str, decision: str) -> ApprovalRecord:
        ...

    def register_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        ...

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        ...

    def get_artifact(self, run_id: str, artifact_id: str) -> ArtifactRecord | None:
        ...


class InMemoryTaskStore:
    """Replaceable memory store; M07 swaps PostgreSQL behind the same surface."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, SessionRecord] = {}
        self._runs: dict[str, RunRecord] = {}
        self._messages: list[Message] = []
        self._pending: dict[str, list[Message]] = {}
        self._events: dict[str, list[AgentEvent]] = {}
        self._event_seq: dict[str, int] = {}
        self._tool_executions: dict[str, ToolExecutionRecord] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._artifacts: dict[str, ArtifactRecord] = {}
        self._approvals: dict[str, ApprovalRecord] = {}

    def create_session(self, workspace_id: str, *, session_id: str | None = None) -> SessionRecord:
        with self._lock:
            sid = session_id or str(uuid.uuid4())
            if sid in self._sessions:
                raise ValueError(f"session already exists: {sid}")
            record = SessionRecord(session_id=sid, workspace_id=workspace_id)
            self._sessions[sid] = record
            return record

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            return self._sessions.get(session_id)

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
        from review_agent.harness.review_state import initial_review_state

        acceptance, review_target = normalize_task_request(task_mode, review_target, acceptance, reviewer)

        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"unknown session: {session_id}")
            if idempotency_key:
                existing_id = self._idempotency.get((session_id, idempotency_key))
                if existing_id:
                    existing = self._runs[existing_id]
                    if not same_run_request(existing, requirement, profile_id, acceptance, reviewer, task_mode, review_target):
                        raise ValueError("idempotency key conflict")
                    return existing
            if self.active_run(session_id) is not None:
                raise RuntimeError("session already has an active run")
            rid = run_id or str(uuid.uuid4())
            if rid in self._runs:
                raise ValueError(f"run already exists: {rid}")
            record = RunRecord(
                run_id=rid,
                session_id=session_id,
                status=RunStatus.QUEUED,
                requirement=requirement,
                profile_id=profile_id,
                acceptance=acceptance or AcceptanceSpec(),
                idempotency_key=idempotency_key,
                review=initial_review_state("off" if task_mode == "plan" else reviewer),
                task_mode=task_mode,
                review_target=review_target,
            )
            return self._finish_create_run(record, idempotency_key)

    def _finish_create_run(self, record: RunRecord, idempotency_key: str | None) -> RunRecord:
        self._runs[record.run_id] = record
        self._events[record.run_id] = []
        self._event_seq[record.run_id] = 0
        self._pending[record.run_id] = []
        if idempotency_key:
            self._idempotency[(record.session_id, idempotency_key)] = record.run_id
        return record

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

        acceptance, review_target = normalize_task_request(task_mode, review_target, acceptance, reviewer)

        with self._lock:
            if session_id not in self._sessions:
                raise KeyError(f"unknown session: {session_id}")
            existing_id = self._idempotency.get((session_id, idempotency_key))
            if existing_id:
                existing = self._runs[existing_id]
                if not same_run_request(existing, requirement, profile_id, acceptance, reviewer, task_mode, review_target):
                    raise ValueError("idempotency key conflict")
                return existing
            if self.active_run(session_id) is not None:
                raise RuntimeError("session already has an active run")
            rid = run_id or str(uuid.uuid4())
            record = RunRecord(
                run_id=rid,
                session_id=session_id,
                status=RunStatus.QUEUED,
                requirement=requirement,
                profile_id=profile_id,
                acceptance=acceptance or AcceptanceSpec(),
                idempotency_key=idempotency_key,
                dispatch_pending=True,
                workspace_snapshot=workspace_snapshot,
                review=initial_review_state("off" if task_mode == "plan" else reviewer),
                task_mode=task_mode,
                review_target=review_target,
            )
            self._finish_create_run(record, idempotency_key)
            mid = message_id or str(uuid.uuid4())
            self.append_message(
                Message(
                    role="user",
                    content=requirement,
                    message_id=mid,
                    session_id=session_id,
                    run_id=rid,
                )
            )
            payload = {"requirement": requirement}
            if request_summary:
                payload["request_summary"] = request_summary
            self.append_event(
                AgentEvent(type="run.created", message=f"Run created for session {session_id}", run_id=rid, payload=payload)
            )
            return record

    def list_sessions(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[SessionRecord], str | None]:
        with self._lock:
            items = sorted(self._sessions.values(), key=lambda item: (item.created_at, item.session_id))
            if cursor:
                created_at, session_id = _split_cursor(cursor)
                items = [
                    item
                    for item in items
                    if (item.created_at, item.session_id) > (created_at, session_id)
                ]
            page = items[: max(1, min(limit, 100))]
            next_cursor = None
            if len(items) > len(page):
                last = page[-1]
                next_cursor = f"{last.created_at}|{last.session_id}"
            return list(page), next_cursor

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def list_runs(self, session_id: str) -> list[RunRecord]:
        with self._lock:
            return [run for run in self._runs.values() if run.session_id == session_id]

    def active_run(self, session_id: str) -> RunRecord | None:
        with self._lock:
            for run in self._runs.values():
                if run.session_id == session_id and run.status in ACTIVE_RUN_STATUSES:
                    return run
            return None

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
        with self._lock:
            run = self._runs[run_id]
            if workspace_snapshot is not None:
                run.workspace_snapshot = dict(workspace_snapshot)
            if status is not None:
                run.status = status
            if cancel_requested is not None:
                run.cancel_requested = cancel_requested
            if workspace_revision is not None:
                run.workspace_revision = workspace_revision
            if verification is not None:
                run.verification = verification
            if dispatch_pending is not None:
                run.dispatch_pending = dispatch_pending
            if usage is not None:
                run.usage = usage
            if budget is not None:
                run.budget = budget
            if owner_id is not None:
                run.owner_id = owner_id
            if lease_until is not None:
                run.lease_until = lease_until
            if heartbeat_at is not None:
                run.heartbeat_at = heartbeat_at
            if attention is not None:
                run.attention = attention
            if review is not None:
                run.review = review
            if clear_owner:
                run.owner_id = None
                run.lease_until = None
                run.heartbeat_at = None
            run.updated_at = _now()
            return run

    def list_dispatch_pending(self) -> list[RunRecord]:
        with self._lock:
            return [run for run in self._runs.values() if run.dispatch_pending]

    def claim_run(
        self,
        run_id: str,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> RunRecord | None:
        from review_agent.services.lease import lease_expired, lease_until_iso, run_is_claimable, utcnow

        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return None
            approvals = [item for item in self._approvals.values() if item.run_id == run_id]
            if not run_is_claimable(run, approvals=approvals):
                return None
            if run.owner_id and run.owner_id != owner_id and not lease_expired(run.lease_until):
                return None
            now = utcnow()
            run.owner_id = owner_id
            run.lease_until = lease_until_iso(lease_seconds, now=now)
            run.heartbeat_at = now.isoformat()
            run.status = RunStatus.RUNNING
            run.dispatch_pending = False
            run.updated_at = _now()
            return run

    def heartbeat_run(self, run_id: str, owner_id: str, *, lease_seconds: int) -> RunRecord | None:
        from review_agent.services.lease import lease_until_iso, utcnow

        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.owner_id != owner_id:
                return None
            now = utcnow()
            run.lease_until = lease_until_iso(lease_seconds, now=now)
            run.heartbeat_at = now.isoformat()
            run.updated_at = _now()
            return run

    def release_run(self, run_id: str, owner_id: str) -> RunRecord | None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None or run.owner_id != owner_id:
                return None
            run.owner_id = None
            run.lease_until = None
            run.heartbeat_at = None
            run.updated_at = _now()
            return run

    def get_tool_execution(self, call_id: str) -> ToolExecutionRecord | None:
        with self._lock:
            return self._tool_executions.get(call_id)

    def list_tool_executions(self, run_id: str) -> list[ToolExecutionRecord]:
        with self._lock:
            return [item for item in self._tool_executions.values() if item.run_id == run_id]

    def append_message(self, message: Message) -> Message:
        with self._lock:
            mid = message.message_id or str(uuid.uuid4())
            for existing in self._messages:
                if existing.message_id == mid:
                    return existing
            stored = Message(
                role=message.role,
                content=message.content,
                message_id=mid,
                session_id=message.session_id,
                run_id=message.run_id,
                tool_calls=message.tool_calls,
                provider_call_id=message.provider_call_id,
            )
            self._messages.append(stored)
            return stored

    def list_messages(
        self,
        session_id: str,
        *,
        before_run_id: str | None = None,
        include_run_id: str | None = None,
    ) -> list[Message]:
        with self._lock:
            runs = {run.run_id: run for run in self._runs.values() if run.session_id == session_id}
            allowed_run_ids: set[str] | None = None
            if before_run_id is not None:
                cutoff = runs[before_run_id].created_at
                allowed_run_ids = {
                    rid for rid, run in runs.items() if run.created_at < cutoff or rid == before_run_id
                }
            if include_run_id is not None:
                allowed_run_ids = (allowed_run_ids or set(runs)) | {include_run_id}
            result: list[Message] = []
            for message in self._messages:
                if message.session_id != session_id:
                    continue
                if allowed_run_ids is not None and message.run_id and message.run_id not in allowed_run_ids:
                    continue
                result.append(message)
            return list(result)

    def enqueue_pending(self, run_id: str, message: Message) -> Message:
        with self._lock:
            if run_id not in self._runs:
                raise KeyError(f"unknown run: {run_id}")
            stored = self.append_message(message)
            pending = self._pending.setdefault(run_id, [])
            if any(item.message_id == stored.message_id for item in pending):
                return stored
            pending.append(stored)
            return stored

    def drain_pending(self, run_id: str) -> list[Message]:
        with self._lock:
            pending = self._pending.get(run_id, [])
            self._pending[run_id] = []
            return list(pending)

    def append_event(self, event: AgentEvent) -> AgentEvent:
        with self._lock:
            run_id = event.run_id
            if not run_id or run_id not in self._runs:
                raise KeyError(f"unknown run for event: {run_id}")
            seq = self._event_seq[run_id] + 1
            self._event_seq[run_id] = seq
            stored = AgentEvent(
                type=event.type,
                message=event.message,
                tool_call=event.tool_call,
                tool_result=event.tool_result,
                approval_request=event.approval_request,
                run_id=run_id,
                seq=seq,
                payload=dict(event.payload),
            )
            self._events[run_id].append(stored)
            return stored

    def list_events(self, run_id: str, *, after_seq: int = 0) -> list[AgentEvent]:
        with self._lock:
            return [event for event in self._events.get(run_id, []) if (event.seq or 0) > after_seq]

    def record_tool_execution(
        self,
        *,
        run_id: str,
        session_id: str,
        tool_call: ToolCall,
        result: ToolResult | None = None,
        replay_category: str | ReplayCategory | None = None,
    ) -> ToolExecutionRecord:
        with self._lock:
            call_id = tool_call.call_id or str(uuid.uuid4())
            if call_id in self._tool_executions and result is None:
                return self._tool_executions[call_id]
            category = replay_category.value if isinstance(replay_category, ReplayCategory) else replay_category
            record = ToolExecutionRecord(
                call_id=call_id,
                run_id=run_id,
                session_id=session_id,
                tool_call=ToolCall(
                    name=tool_call.name,
                    arguments=tool_call.arguments,
                    call_id=call_id,
                    provider_call_id=tool_call.provider_call_id,
                ),
                result=result,
                replay_category=category,
            )
            self._tool_executions[call_id] = record
            return record

    def complete_tool_execution(
        self,
        call_id: str,
        result: ToolResult | None,
        *,
        execution_meta: dict[str, Any] | None = None,
    ) -> ToolExecutionRecord:
        with self._lock:
            record = self._tool_executions[call_id]
            record.result = result
            if execution_meta:
                record.execution_id = execution_meta.get("execution_id") or record.execution_id
                record.argv = execution_meta.get("argv") or record.argv
                record.started_at = execution_meta.get("started_at") or record.started_at
                record.finished_at = execution_meta.get("finished_at") or record.finished_at
                record.pid = execution_meta.get("pid", record.pid)
                record.pgid = execution_meta.get("pgid", record.pgid)
                record.container_name = execution_meta.get("container_name") or record.container_name
                record.container_id = execution_meta.get("container_id") or record.container_id
                record.workspace_revision = (
                    execution_meta.get("workspace_revision") or record.workspace_revision
                )
                record.stdout_artifact = execution_meta.get("stdout_artifact") or record.stdout_artifact
                record.stderr_artifact = execution_meta.get("stderr_artifact") or record.stderr_artifact
                record.patch_hash = execution_meta.get("patch_hash") or record.patch_hash
                record.execution_status = execution_meta.get("status") or record.execution_status
            return record

    def register_artifact(self, record: ArtifactRecord) -> ArtifactRecord:
        with self._lock:
            for existing in self._artifacts.values():
                if existing.run_id == record.run_id and existing.storage_path == record.storage_path:
                    return existing
            self._artifacts[record.artifact_id] = record
            return record

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._lock:
            return [item for item in self._artifacts.values() if item.run_id == run_id]

    def get_artifact(self, run_id: str, artifact_id: str) -> ArtifactRecord | None:
        with self._lock:
            record = self._artifacts.get(artifact_id)
            if record is None or record.run_id != run_id:
                return None
            return record

    def create_approval(self, record: ApprovalRecord) -> ApprovalRecord:
        with self._lock:
            existing = self._approvals.get(record.approval_id)
            if existing is not None:
                return existing
            self._approvals[record.approval_id] = record
            return record

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self._approvals.get(approval_id)

    def list_approvals(self, run_id: str) -> list[ApprovalRecord]:
        with self._lock:
            return [item for item in self._approvals.values() if item.run_id == run_id]

    def set_approval_decision(self, approval_id: str, decision: str) -> ApprovalRecord:
        with self._lock:
            record = self._approvals[approval_id]
            if record.decision is not None and record.decision != decision:
                raise ValueError("approval decision conflict")
            record.decision = decision
            record.decided_at = _now()
            return record


def _split_cursor(cursor: str) -> tuple[str, str]:
    created_at, _, ident = cursor.partition("|")
    if not ident:
        raise ValueError("invalid cursor")
    return created_at, ident


def normalize_task_request(
    task_mode: TaskMode,
    review_target: ReviewTarget | None,
    acceptance: AcceptanceSpec | None,
    reviewer: str,
) -> tuple[AcceptanceSpec, ReviewTarget | None]:
    if task_mode not in {"develop", "review", "plan"}:
        raise ValueError("task_mode must be develop, review or plan")
    if reviewer not in {"default", "off"}:
        raise ValueError("reviewer must be default or off")
    target = ReviewTarget.model_validate(review_target).model_copy(deep=True) if review_target is not None else None
    if task_mode != "review" and target is not None:
        raise ValueError("review_target is only supported for review tasks")
    spec = acceptance or AcceptanceSpec()
    if task_mode != "develop":
        if spec.checks or spec.mode not in {"unverified", "not_applicable"}:
            raise ValueError("read-only tasks cannot run acceptance commands")
        spec = AcceptanceSpec(mode="not_applicable", description=spec.description)
    if task_mode == "review":
        if reviewer == "off":
            raise ValueError("review tasks require Reviewer")
        target = target or ReviewTarget(kind="workspace_changes")
    return spec, target


def same_run_request(
    run: RunRecord,
    requirement: str,
    profile_id: str,
    acceptance: AcceptanceSpec,
    reviewer: str,
    task_mode: TaskMode,
    review_target: ReviewTarget | None,
) -> bool:
    return (run.requirement == requirement and run.profile_id == profile_id
            and run.acceptance == acceptance
            and (run.review or {}).get("mode", "default") == ("off" if task_mode == "plan" else reviewer)
            and run.task_mode == task_mode and run.review_target == review_target)
