from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from review_agent.config import Settings, get_settings
from review_agent.db.codec import artifact_kind_for, file_sha256, merge_usage
from review_agent.errors import (
    ApprovalConflictError,
    ApprovalStaleError,
    IdempotencyConflictError,
    InvalidWorkspaceError,
    ReconciliationRequiredError,
    ResumeNotEligibleError,
    SessionBusyError,
    TaskNotFoundError,
    TerminalRunError,
)
from review_agent.harness.models import (
    ACTIVE_RUN_STATUSES,
    AgentEvent,
    AgentFinal,
    Message,
    RunStatus,
    ToolResult,
    ToolRisk,
)
from review_agent.harness.runtime import AgentRuntime, prepare_run_source
from review_agent.harness.models import TaskMode
from review_agent.harness.review_target import ReviewTarget
from review_agent.harness.task_store import (
    AcceptanceSpec,
    ArtifactRecord,
    RunRecord,
    SessionRecord,
    TaskStore,
)
from review_agent.services.dispatch import DispatchPublisher, NoOpPublisher, publisher_from_settings
from review_agent.services.workspace_manager import RegisteredWorkspace, WorkspaceManager, WorkspaceError
from review_agent.services.run_lock import run_file_lock

Page = tuple[list[Any], str | None]


class TaskService:
    """Create, query, and execute coding runs. API maps IO only."""

    def __init__(
        self,
        store: TaskStore,
        workspace_manager: WorkspaceManager,
        settings: Settings | None = None,
        *,
        model_client: Any | None = None,
        model_client_factory: Callable[[], Any] | None = None,
        publisher: DispatchPublisher | None = None,
    ) -> None:
        self.store = store
        self.workspace_manager = workspace_manager
        self.settings = settings or get_settings()
        self.model_client = model_client
        self.model_client_factory = model_client_factory
        self.publisher = publisher if publisher is not None else publisher_from_settings(self.settings)
        self._live_runtimes: dict[str, AgentRuntime] = {}
        self._memory_checkpointer: Any = None

    def list_workspaces(self) -> list[RegisteredWorkspace]:
        return self.workspace_manager.list_workspaces()

    def create_session(self, workspace_id: str) -> SessionRecord:
        registered = self.workspace_manager.get_registered(workspace_id)
        if registered is None:
            raise InvalidWorkspaceError(
                f"unknown workspace_id: {workspace_id}",
                public_message="Workspace is not registered.",
            )
        if not registered.path.is_dir():
            raise InvalidWorkspaceError(
                f"workspace path missing: {workspace_id}",
                public_message="Registered workspace path is not a directory.",
            )
        return self.store.create_session(workspace_id)

    def list_sessions(self, *, limit: int = 20, cursor: str | None = None) -> tuple[list[SessionRecord], str | None]:
        return self.store.list_sessions(limit=limit, cursor=cursor)

    def get_session(
        self,
        session_id: str,
        *,
        message_limit: int = 50,
        message_cursor: str | None = None,
    ) -> dict[str, Any]:
        session = self.store.get_session(session_id)
        if session is None:
            raise TaskNotFoundError(f"unknown session: {session_id}", public_message="Session not found.")
        messages = self.store.list_messages(session_id)
        page, next_cursor = _page_messages(messages, limit=message_limit, cursor=message_cursor)
        active = self.store.active_run(session_id)
        return {
            "session": session,
            "messages": page,
            "next_cursor": next_cursor,
            "active_run_id": active.run_id if active else None,
        }

    def list_session_runs(
        self,
        session_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> tuple[list[RunRecord], str | None]:
        if self.store.get_session(session_id) is None:
            raise TaskNotFoundError(f"unknown session: {session_id}", public_message="Session not found.")
        runs = self.store.list_runs(session_id)
        return _page_runs(runs, limit=limit, cursor=cursor)

    def create_run(
        self,
        session_id: str,
        requirement: str,
        *,
        idempotency_key: str,
        profile_id: str = "deepseek",
        acceptance: AcceptanceSpec | None = None,
        message_id: str | None = None,
        reviewer: str = "default",
        task_mode: TaskMode = "develop",
        review_target: ReviewTarget | None = None,
    ) -> RunRecord:
        session = self.store.get_session(session_id)
        if session is None:
            raise TaskNotFoundError(f"unknown session: {session_id}", public_message="Session not found.")
        registered = self.workspace_manager.get_registered(session.workspace_id)
        snapshot = None
        if registered is not None:
            snapshot = {
                "workspace_id": registered.workspace_id,
                "display_name": registered.display_name,
                "path": str(registered.path),
            }
        summary = {"requirement": requirement, "profile_id": profile_id, "reviewer": reviewer, "task_mode": task_mode,
                   "review_target": review_target.model_dump(mode="json") if review_target else None}
        candidate_id = str(uuid.uuid4())
        try:
            # Hold the existing worker lock before the queued row can be dispatched.
            with run_file_lock(self.workspace_manager.data_root, candidate_id):
                run = self.store.create_run_bundle(
                    session_id,
                    requirement,
                    idempotency_key=idempotency_key,
                    profile_id=profile_id,
                    acceptance=acceptance,
                    message_id=message_id,
                    workspace_snapshot=snapshot,
                    request_summary=summary,
                    run_id=candidate_id,
                    reviewer=reviewer,
                    task_mode=task_mode,
                    review_target=review_target,
                )
                if run.run_id == candidate_id:
                    try:
                        prepare_run_source(self.store, self.workspace_manager, session.workspace_id, run.run_id)
                    except (OSError, ValueError, WorkspaceError) as exc:
                        self.store.update_run(run.run_id, status=RunStatus.FAILED, dispatch_pending=False)
                        self.store.append_event(AgentEvent(type="run.failed", run_id=run.run_id,
                            message="Input snapshot could not be prepared; source changed or is unavailable."))
                        raise InvalidWorkspaceError(str(exc), public_message="Input snapshot failed; retry with a new task.") from exc
        except ValueError as exc:
            if "idempotency" in str(exc).lower():
                raise IdempotencyConflictError(str(exc)) from exc
            raise
        except RuntimeError as exc:
            if "active run" in str(exc).lower():
                raise SessionBusyError(str(exc)) from exc
            raise
        if run.dispatch_pending:
            self._publish_run(run.run_id)
        return self.store.get_run(run.run_id) or run

    def get_run(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        if run is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        events = self.store.list_events(run_id)
        last_type = events[-1].type if events else None
        approvals = self.store.list_approvals(run_id)
        pending_approval = next((item for item in approvals if item.decision is None), None)
        return {
            "run": run,
            "phase": last_type or run.status.value,
            "pending_approval_id": pending_approval.approval_id if pending_approval else None,
            "pending_approval": pending_approval,
            "needs_attention": run.status == RunStatus.NEEDS_ATTENTION,
            "attention": run.attention,
            "review": run.review,
        }

    def append_message(self, run_id: str, content: str, *, message_id: str | None = None) -> Message:
        run = self.store.get_run(run_id)
        if run is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        if run.status not in ACTIVE_RUN_STATUSES:
            raise TerminalRunError(
                "cannot append message to terminal run; create a new run",
            )
        message = Message(
            role="user",
            content=content,
            message_id=message_id or str(uuid.uuid4()),
            session_id=run.session_id,
            run_id=run_id,
        )
        stored = self.store.enqueue_pending(run_id, message)
        self.store.append_event(
            AgentEvent(
                type="message.received",
                message=f"Queued supplemental message {stored.message_id}",
                run_id=run_id,
                payload={"message_id": stored.message_id},
            )
        )
        return stored

    def request_cancel(self, run_id: str) -> RunRecord:
        run = self.store.get_run(run_id)
        if run is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        if run.status not in ACTIVE_RUN_STATUSES:
            return run
        if run.cancel_requested:
            return run
        updated = self.store.update_run(run_id, cancel_requested=True)
        self.store.append_event(
            AgentEvent(type="run.cancel_requested", message="Cancel requested.", run_id=run_id)
        )
        runtime = self._live_runtimes.get(run_id)
        if runtime is not None:
            runtime.propagate_cancel(run_id)
        return updated

    def resume_run(
        self,
        run_id: str,
        *,
        action: str = "continue",
        call_id: str | None = None,
        workspace_revision: str | None = None,
    ) -> RunRecord:
        from review_agent.harness.workspace_revision import compute_workspace_revision
        from review_agent.services.recovery import mark_needs_attention, reconcile_run

        run = self.store.get_run(run_id)
        if run is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        if run.status not in ACTIVE_RUN_STATUSES:
            raise TerminalRunError("cannot resume a terminal run; create a new run")
        if run.status == RunStatus.WAITING_APPROVAL:
            raise ResumeNotEligibleError(
                "use the approval decision endpoint",
                public_message="This run is waiting for an approval decision.",
            )
        if run.status == RunStatus.RUNNING and run.owner_id:
            raise ResumeNotEligibleError(
                "run is currently executing",
                public_message="The run is still executing.",
            )
        prepared = self.workspace_manager.get_run(run_id)
        source = prepared.source_root if prepared is not None else None
        actual_revision = compute_workspace_revision(source) if source is not None else None
        if workspace_revision and actual_revision and workspace_revision != actual_revision:
            raise ReconciliationRequiredError(
                "workspace revision mismatch",
                public_message="Observed workspace revision does not match the current files.",
                metadata={"expected": workspace_revision, "actual": actual_revision},
            )
        if run.status == RunStatus.NEEDS_ATTENTION:
            if action not in {"accept_and_continue", "end_task"}:
                raise ReconciliationRequiredError(
                    "needs_attention requires accept_and_continue or end_task",
                    public_message="Unknown side effects must be reconciled before the run can continue.",
                    metadata=run.attention or {},
                )
            attention = dict(run.attention or {})
            attention["action"] = action
            attention["call_id"] = call_id or attention.get("call_id")
            blocked_call_id = attention.get("call_id")
            if blocked_call_id:
                existing = self.store.get_tool_execution(str(blocked_call_id))
                if existing is not None and existing.result is None:
                    self.store.complete_tool_execution(
                        str(blocked_call_id),
                        ToolResult(
                            success=True,
                            summary="Manually reconciled: user accepted the current file state.",
                            call_id=str(blocked_call_id),
                            risk_level=ToolRisk.WRITE_CONFIRM,
                        ),
                        execution_meta={"status": "succeeded", "reconciled_manually": True},
                    )
            if action == "end_task":
                self.store.update_run(
                    run_id,
                    status=RunStatus.CANCELLED if run.cancel_requested else RunStatus.FAILED,
                    attention=attention,
                    cancel_requested=run.cancel_requested,
                    clear_owner=True,
                )
                self.store.append_event(
                    AgentEvent(
                        type="run.reconciliation",
                        message="Task ended after manual reconciliation.",
                        run_id=run_id,
                        payload=attention,
                    )
                )
                return self.store.get_run(run_id) or run
            note = (
                "User accepted the current file state and asked to continue verification. "
                "Unknown command exit codes remain unknown; verification stays unverified."
            )
            self.store.enqueue_pending(
                run_id,
                Message(
                    role="user",
                    content=note,
                    message_id=str(uuid.uuid4()),
                    session_id=run.session_id,
                    run_id=run_id,
                ),
            )
            self.store.update_run(
                run_id,
                status=RunStatus.INTERRUPTED,
                attention=attention,
                dispatch_pending=True,
            )
            self.store.append_event(
                AgentEvent(type="run.reconciliation", message=note, run_id=run_id, payload=attention)
            )
            self._publish_run(run_id)
            return self.store.get_run(run_id) or run
        if source is not None:
            decision = reconcile_run(store=self.store, workspace_root=source, run_id=run_id)
            if decision.status == "needs_attention":
                mark_needs_attention(self.store, run_id, decision)
                raise ReconciliationRequiredError(
                    decision.reason,
                    public_message=decision.reason,
                    metadata=decision.as_attention(),
                )
        self.store.update_run(run_id, status=RunStatus.INTERRUPTED, dispatch_pending=True)
        self._publish_run(run_id)
        return self.store.get_run(run_id) or run

    def decide_approval(self, approval_id: str, decision: str) -> None:
        if decision not in {"allow", "deny"}:
            raise ResumeNotEligibleError(f"unsupported decision: {decision}")
        record = self.store.get_approval(approval_id)
        if record is None:
            raise TaskNotFoundError(f"unknown approval: {approval_id}", public_message="Approval not found.")
        prepared = self.workspace_manager.get_run(record.run_id)
        if prepared is not None:
            from review_agent.harness.workspace_revision import compute_workspace_revision

            current = compute_workspace_revision(prepared.source_root)
            if record.workspace_revision and record.workspace_revision != current:
                raise ApprovalStaleError(
                    "workspace revision changed since approval intent",
                    metadata={
                        "approval_id": approval_id,
                        "bound_revision": record.workspace_revision,
                        "current_revision": current,
                    },
                )
        try:
            self.store.set_approval_decision(approval_id, decision)
        except ValueError as exc:
            raise ApprovalConflictError(str(exc)) from exc
        self.store.update_run(record.run_id, dispatch_pending=True, status=RunStatus.WAITING_APPROVAL)
        self._publish_run(record.run_id)

    def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        if self.store.get_run(run_id) is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        return self.store.list_artifacts(run_id)

    def get_artifact_file(self, run_id: str, artifact_id: str) -> tuple[ArtifactRecord, Path]:
        if self.store.get_run(run_id) is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        record = self.store.get_artifact(run_id, artifact_id)
        if record is None:
            raise TaskNotFoundError("artifact not found", public_message="Artifact not found.")
        path = (self.workspace_manager.data_root / record.storage_path).resolve()
        try:
            path.relative_to(self.workspace_manager.data_root.resolve())
        except ValueError as exc:
            raise TaskNotFoundError("artifact not found", public_message="Artifact not found.") from exc
        if not path.is_file():
            raise TaskNotFoundError("artifact not found", public_message="Artifact not found.")
        return record, path

    def iter_events(self, run_id: str, *, after_seq: int = 0) -> list[AgentEvent]:
        if self.store.get_run(run_id) is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        return self.store.list_events(run_id, after_seq=after_seq)

    def event_stream(self, run_id: str, *, after_seq: int = 0) -> Iterator[str]:
        if self.store.get_run(run_id) is None:
            raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
        last = after_seq
        idle = 0
        while True:
            events = self.store.list_events(run_id, after_seq=last)
            for event in events:
                last = int(event.seq or last)
                payload = {
                    "type": event.type,
                    "message": event.message,
                    "payload": event.payload,
                    "run_id": event.run_id,
                    "seq": event.seq,
                }
                yield f"id: {event.seq}\nevent: {event.type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                idle = 0
            run = self.store.get_run(run_id)
            if run is not None and run.status not in ACTIVE_RUN_STATUSES:
                return
            idle += 1
            yield ": heartbeat\n\n"
            time.sleep(0.4)
            if idle > 500:
                return

    def run_once(
        self,
        run_id: str,
        *,
        approver: Callable | None = None,
        owner_id: str | None = None,
    ) -> AgentFinal:
        from review_agent.services.run_attempt import execute_run_attempt

        return execute_run_attempt(self, run_id, approver=approver, owner_id=owner_id)

    def scan_dispatch_pending(self) -> int:
        count = 0
        for run in self.store.list_dispatch_pending():
            self._publish_run(run.run_id)
            count += 1
        return count

    def _publish_run(self, run_id: str) -> None:
        if isinstance(self.publisher, NoOpPublisher):
            return
        try:
            self.publisher.publish(run_id)
        except Exception:
            return
        self.store.update_run(run_id, dispatch_pending=False)

    def _resume_value(self, run_id: str) -> Any:
        decided = [item for item in self.store.list_approvals(run_id) if item.decision]
        if not decided:
            return None
        return decided[-1].decision

    def _register_run_artifacts(self, run_id: str) -> None:
        prepared = self.workspace_manager.get_run(run_id)
        if prepared is None:
            return
        root = prepared.artifacts_root
        data_root = self.workspace_manager.data_root.resolve()
        if not root.is_dir():
            return
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            rel = resolved.relative_to(data_root).as_posix()
            body = resolved.read_bytes()
            digest = file_sha256(body)
            record = ArtifactRecord(
                artifact_id=str(uuid.uuid4()),
                run_id=run_id,
                kind=artifact_kind_for(path.name),
                summary=path.name,
                size_bytes=len(body),
                content_hash=digest,
                storage_path=rel,
            )
            self.store.register_artifact(record)


def accumulate_run_usage(store: TaskStore, run_id: str, usage: Any, *, step_count: int, max_steps: int) -> None:
    incoming = None
    if usage is not None:
        incoming = usage.model_dump(mode="json") if hasattr(usage, "model_dump") else {
            "profile_id": getattr(usage, "profile_id", None),
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "model_latency_ms": getattr(usage, "model_latency_ms", None),
            "estimated_cost": getattr(usage, "estimated_cost", None),
            "price_config_id": getattr(usage, "price_config_id", None),
            "price_as_of": getattr(usage, "price_as_of", None),
        }
    run = store.get_run(run_id)
    merged = merge_usage(run.usage if run else None, incoming)
    store.update_run(
        run_id,
        usage=merged,
        budget={"max_steps": max_steps, "step_count": step_count},
    )


def _page_messages(
    messages: list[Message],
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[Message], str | None]:
    limit = max(1, min(limit, 100))
    items = list(messages)
    if cursor:
        items = [item for item in items if item.message_id > cursor]
    page = items[:limit]
    next_cursor = page[-1].message_id if len(items) > len(page) else None
    return page, next_cursor


def _page_runs(
    runs: list[RunRecord],
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[RunRecord], str | None]:
    limit = max(1, min(limit, 100))
    items = sorted(runs, key=lambda item: (item.created_at, item.run_id))
    if cursor:
        created_at, run_id = cursor.partition("|")[0], cursor.partition("|")[2]
        if not run_id:
            raise ValueError("invalid cursor")
        items = [item for item in items if (item.created_at, item.run_id) > (created_at, run_id)]
    page = items[:limit]
    next_cursor = None
    if len(items) > len(page):
        last = page[-1]
        next_cursor = f"{last.created_at}|{last.run_id}"
    return page, next_cursor
