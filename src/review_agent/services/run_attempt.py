from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from review_agent.errors import InvalidWorkspaceError, RunLeaseHeldError, RunLockHeldError, TaskNotFoundError
from review_agent.harness.models import ACTIVE_RUN_STATUSES, AgentEvent, AgentFinal, RunStatus
from review_agent.harness.runtime import AgentRuntime
from review_agent.services.orphans import stop_orphans
from review_agent.services.recovery import ReconcileDecision, mark_needs_attention, reconcile_run
from review_agent.services.run_lock import RunLockHeld, run_file_lock

if TYPE_CHECKING:
    from review_agent.services.task_service import TaskService

Approver = Callable | None


def execute_run_attempt(
    service: TaskService,
    run_id: str,
    *,
    approver: Approver = None,
    owner_id: str | None = None,
) -> AgentFinal:
    """Claim, lock, reconcile, and drive one coding run attempt."""
    run = service.store.get_run(run_id)
    if run is None:
        raise TaskNotFoundError(f"unknown run: {run_id}", public_message="Run not found.")
    session = service.store.get_session(run.session_id)
    if session is None:
        raise TaskNotFoundError(f"unknown session: {run.session_id}", public_message="Session not found.")
    registered = service.workspace_manager.get_registered(session.workspace_id)
    if registered is None:
        raise InvalidWorkspaceError(
            f"unknown workspace_id: {session.workspace_id}",
            public_message="Workspace is not registered.",
        )
    model = service.model_client
    if model is None and service.model_client_factory is not None:
        model = service.model_client_factory()
    if model is None:
        from review_agent.harness.model_client import OpenAICompatibleModelClient

        model = OpenAICompatibleModelClient(settings=service.settings)

    if owner_id:
        claimed = service.store.claim_run(
            run_id, owner_id, lease_seconds=int(service.settings.review_agent_lease_seconds)
        )
        if claimed is None:
            raise RunLeaseHeldError(f"could not claim run {run_id}")
    else:
        service.store.update_run(run_id, dispatch_pending=False)

    resume_value = service._resume_value(run_id)
    stop_heartbeat = threading.Event()
    lease_seconds = int(service.settings.review_agent_lease_seconds)

    def _heartbeat() -> None:
        while owner_id and not stop_heartbeat.wait(max(5, lease_seconds / 3)):
            service.store.heartbeat_run(run_id, owner_id, lease_seconds=lease_seconds)

    def _watch_cancel(runtime: AgentRuntime) -> None:
        while not stop_heartbeat.wait(0.2):
            current = service.store.get_run(run_id)
            if current is None:
                return
            if current.cancel_requested:
                runtime.propagate_cancel(run_id)
                return
            if current.status not in ACTIVE_RUN_STATUSES:
                return

    def _execute() -> AgentFinal:
        if not (service.settings.review_agent_database_url or "").strip():
            if service._memory_checkpointer is None:
                from langgraph.checkpoint.memory import InMemorySaver

                service._memory_checkpointer = InMemorySaver()
        runtime = AgentRuntime(
            registered.path,
            model_client=model,
            settings=service.settings,
            task_store=service.store,
            workspace_manager=service.workspace_manager,
            isolate_workspace=True,
            workspace_id=session.workspace_id,
            profile_id=run.profile_id,
            checkpointer=service._memory_checkpointer,
        )
        prepared = runtime.attach_or_prepare_workspace(run_id)
        records = service.store.list_tool_executions(run_id)
        orphan = stop_orphans(executor=runtime.executor, records=records, workspace=prepared)
        if orphan.has_unknown:
            mark_needs_attention(
                service.store,
                run_id,
                ReconcileDecision(
                    status="needs_attention",
                    reason="Could not confirm that the previous command or container has exited.",
                    payload={"unknown": orphan.unknown},
                ),
            )
            return AgentFinal(
                message="",
                run_id=run_id,
                session_id=run.session_id,
                run_status=RunStatus.NEEDS_ATTENTION.value,
            )
        if prepared is not None:
            decision = reconcile_run(store=service.store, workspace_root=prepared.source_root, run_id=run_id)
            if decision.status == "needs_attention":
                mark_needs_attention(service.store, run_id, decision)
                return AgentFinal(
                    message=decision.reason,
                    run_id=run_id,
                    session_id=run.session_id,
                    run_status=RunStatus.NEEDS_ATTENTION.value,
                )
            attention = (service.store.get_run(run_id) or run).attention or {}
            if attention.get("action") == "end_task":
                status = RunStatus.CANCELLED if run.cancel_requested else RunStatus.FAILED
                service.store.update_run(run_id, status=status)
                return AgentFinal(
                    message="",
                    run_id=run_id,
                    session_id=run.session_id,
                    run_status=status.value,
                )
        service._live_runtimes[run_id] = runtime
        hb = threading.Thread(target=_heartbeat, daemon=True)
        watch = threading.Thread(target=_watch_cancel, args=(runtime,), daemon=True)
        hb.start()
        watch.start()
        async def execute_and_close():
            try:
                return await runtime.execute_run(run_id, approver=approver, resume=resume_value)
            finally:
                # Close clients owned by this attempt before asyncio.run closes its loop.
                if service.model_client is None and hasattr(model, "aclose"):
                    await model.aclose()

        try:
            result = _run_async(execute_and_close())
        except Exception as exc:
            current = service.store.get_run(run_id)
            if current is not None and current.status == RunStatus.RUNNING:
                service.store.update_run(run_id, status=RunStatus.INTERRUPTED)
                service.store.append_event(AgentEvent(
                    type="run.interrupted", run_id=run_id,
                    message=f"Execution stopped ({type(exc).__name__}); resume will reconcile side effects.",
                ))
            raise
        finally:
            stop_heartbeat.set()
            service._live_runtimes.pop(run_id, None)
        service._register_run_artifacts(run_id)
        return result

    try:
        if owner_id:
            try:
                with run_file_lock(service.workspace_manager.data_root, run_id):
                    return _execute()
            except RunLockHeld as exc:
                service.store.release_run(run_id, owner_id)
                raise RunLockHeldError(str(exc)) from exc
        return _execute()
    finally:
        stop_heartbeat.set()
        if owner_id:
            current = service.store.get_run(run_id)
            if current is not None and current.status in {
                RunStatus.WAITING_APPROVAL,
                RunStatus.INTERRUPTED,
                RunStatus.NEEDS_ATTENTION,
                RunStatus.SUCCEEDED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }:
                service.store.release_run(run_id, owner_id)


def _run_async(coro: Any) -> Any:
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    raise RuntimeError("run_once cannot nest inside a running event loop; await execute_run instead")
