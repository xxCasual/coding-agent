from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from review_agent.config import Settings
from review_agent.harness.models import ModelTurnResult, ReplayCategory, RunStatus, ToolCall
from review_agent.harness.runtime import AgentRuntime
from review_agent.harness.task_store import InMemoryTaskStore
from review_agent.services.patch_apply import FilePatchPlan, PatchPlan, apply_patch, plan_patch
from review_agent.services.recovery import mark_needs_attention, reconcile_run
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager
from review_agent.worker.locks import RunLockHeld, run_file_lock
from tests.test_coding_runtime import FakeAsyncModelClient
from tests.test_task_service import _client, _service, _settings


def test_claim_run_rejects_duplicate_owner(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run_bundle(session.session_id, "fix", idempotency_key="k")
    first = store.claim_run(run.run_id, "w1", lease_seconds=30)
    assert first is not None
    assert store.claim_run(run.run_id, "w2", lease_seconds=30) is None
    store.release_run(run.run_id, "w1")
    assert store.claim_run(run.run_id, "w2", lease_seconds=30) is not None


def test_file_lock_blocks_second_holder(tmp_path: Path) -> None:
    held = threading.Event()
    release = threading.Event()
    error: list[BaseException] = []

    def holder() -> None:
        with run_file_lock(tmp_path, "run-1"):
            held.set()
            release.wait(5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(2)
    with pytest.raises(RunLockHeld):
        with run_file_lock(tmp_path, "run-1"):
            pass
    release.set()
    thread.join()


def test_second_execute_attaches_without_wiping(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("keep\n", encoding="utf-8")
    store = InMemoryTaskStore()
    runtime = AgentRuntime(
        workspace,
        model_client=FakeAsyncModelClient([ModelTurnResult(content="done")]),
        settings=settings,
        task_store=store,
        isolate_workspace=True,
        workspace_id="demo",
    )
    session_id = runtime.create_session()
    run_id = runtime.start_run(session_id, "say done")
    first = runtime.attach_or_prepare_workspace(run_id)
    assert first is not None
    marker = first.source_root / "marker.txt"
    marker.write_text("alive\n", encoding="utf-8")
    second = runtime.attach_or_prepare_workspace(run_id)
    assert second is not None
    assert (second.source_root / "marker.txt").read_text(encoding="utf-8") == "alive\n"
    assert (workspace / "readme.txt").read_text(encoding="utf-8") == "keep\n"


def test_cancel_is_idempotent_and_does_not_rewrite_origin(tmp_path: Path) -> None:
    service = _service(tmp_path)
    origin = tmp_path / "ws"
    origin_text = (origin / "readme.txt").read_text(encoding="utf-8")
    client = _client(tmp_path, service)
    session_id = client.post("/api/sessions", json={"workspace_id": "demo"}).json()["session_id"]
    run_id = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"requirement": "sleep"},
        headers={"Idempotency-Key": "cancel"},
    ).json()["run_id"]
    first = client.post(f"/api/runs/{run_id}/cancel")
    second = client.post(f"/api/runs/{run_id}/cancel")
    assert first.status_code == 200
    assert second.status_code == 200
    events = service.iter_events(run_id)
    assert sum(1 for item in events if item.type == "run.cancel_requested") == 1
    assert (origin / "readme.txt").read_text(encoding="utf-8") == origin_text


def test_cancel_kills_long_command(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    store = InMemoryTaskStore()
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCall(
                        name="run_command",
                        arguments={"argv": ["python", "-c", "import time; time.sleep(20)"]},
                        call_id="sleep1",
                        provider_call_id="sleep1",
                    )
                ]
            ),
            ModelTurnResult(content="done"),
        ]
    )
    runtime = AgentRuntime(
        workspace,
        model_client=model,
        settings=settings,
        task_store=store,
        isolate_workspace=True,
        workspace_id="demo",
    )
    session_id = runtime.create_session()
    run_id = runtime.start_run(session_id, "run a long command")

    def cancel_soon() -> None:
        time.sleep(0.4)
        runtime.request_cancel(run_id)

    thread = threading.Thread(target=cancel_soon)
    thread.start()
    import asyncio

    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _req: True))
    thread.join()
    assert final.run_status in {"cancelled", "needs_attention"}
    handles = runtime.executor.active_for_run(run_id)
    assert handles == []
    assert (workspace / "readme.txt").read_text(encoding="utf-8") == "ok\n"


def test_approval_interrupt_then_decision_resumes(tmp_path: Path) -> None:
    settings = Settings(
        review_agent_data_root=str(tmp_path / "data"),
        review_agent_approval_mode="confirm",
        review_agent_executor_backend="host",
        review_agent_agent_max_steps=8,
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    manager = WorkspaceManager(settings.review_agent_data_root)
    manager.register("demo", workspace, display_name="Demo")
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(
                tool_calls=[
                    ToolCall(
                        name="apply_patch",
                        arguments={
                            "patch": (
                                "diff --git a/readme.txt b/readme.txt\n"
                                "--- a/readme.txt\n"
                                "+++ b/readme.txt\n"
                                "@@ -1 +1 @@\n"
                                "-ok\n"
                                "+fixed\n"
                            )
                        },
                        call_id="p1",
                        provider_call_id="p1",
                    )
                ]
            ),
            ModelTurnResult(content="done"),
        ]
    )
    service = TaskService(InMemoryTaskStore(), manager, settings, model_client=model)
    session = service.create_session("demo")
    run = service.create_run(session.session_id, "patch file", idempotency_key="appr", reviewer="off")
    final = service.run_once(run.run_id)
    assert final.run_status == RunStatus.WAITING_APPROVAL.value
    approvals = service.store.list_approvals(run.run_id)
    assert len(approvals) == 1
    service.decide_approval(approvals[0].approval_id, "allow")
    resumed = service.run_once(run.run_id)
    assert resumed.run_status in {RunStatus.SUCCEEDED.value, RunStatus.FAILED.value, RunStatus.WAITING_APPROVAL.value}
    assert len(service.store.list_approvals(run.run_id)) == 1
    prepared = manager.get_run(run.run_id)
    assert prepared is not None
    assert (prepared.source_root / "readme.txt").read_text(encoding="utf-8") == "fixed\n"
    assert (workspace / "readme.txt").read_text(encoding="utf-8") == "ok\n"


def test_model_timeout_marks_interrupted(tmp_path: Path) -> None:
    class TimeoutModel:
        async def complete(self, messages, tools=None, **kwargs):
            raise TimeoutError("model timeout")

    settings = _settings(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    runtime = AgentRuntime(
        workspace,
        model_client=TimeoutModel(),
        settings=settings,
        task_store=InMemoryTaskStore(),
        isolate_workspace=True,
        workspace_id="demo",
    )
    session_id = runtime.create_session()
    run_id = runtime.start_run(session_id, "hello")
    import asyncio

    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _req: True))
    run = runtime.task_store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.INTERRUPTED
    assert final.run_status == RunStatus.INTERRUPTED.value
    assert any(event.type == "run.interrupted" for event in runtime.task_store.list_events(run_id))


def test_patch_applied_record_missing_is_reconciled(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workspace, check=True, capture_output=True)
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "add", "readme.txt"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=workspace, check=True, capture_output=True)
    patch = (
        "diff --git a/readme.txt b/readme.txt\n"
        "--- a/readme.txt\n"
        "+++ b/readme.txt\n"
        "@@ -1 +1 @@\n"
        "-ok\n"
        "+fixed\n"
    )
    applied = apply_patch(workspace, patch)
    assert applied.success or applied.already_applied
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "patch")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="apply_patch", arguments={"patch": patch}, call_id="p1"),
        replay_category=ReplayCategory.PATCH_CHECKABLE,
    )
    decision = reconcile_run(store=store, workspace_root=workspace, run_id=run.run_id)
    assert decision.status == "ok"
    record = store.get_tool_execution("p1")
    assert record is not None
    assert record.result is not None
    assert record.result.success
    second = apply_patch(workspace, patch)
    assert second.already_applied


def test_stop_orphans_ignores_unstarted_run_command(tmp_path: Path) -> None:
    from review_agent.services.executor import Executor
    from review_agent.services.orphans import stop_orphans

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "cmd")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="run_command", arguments={"argv": ["true"]}, call_id="c1"),
    )
    outcome = stop_orphans(
        executor=Executor(backend="host", artifact_root=artifacts),
        records=store.list_tool_executions(run.run_id),
        workspace=None,
    )
    assert outcome.unknown == []
    assert outcome.has_unknown is False


def test_mcp_disconnect_does_not_replay(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "mcp")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="mcp__other__write", arguments={"x": 1}, call_id="m1"),
    )
    decision = reconcile_run(store=store, workspace_root=tmp_path, run_id=run.run_id)
    assert decision.status == "needs_attention"
    assert decision.call_id == "m1"


def test_sse_reconnect_after_seq(tmp_path: Path) -> None:
    from tests.test_task_service import test_api_sse_resume_after_seq

    test_api_sse_resume_after_seq(tmp_path)


def test_create_run_does_not_execute_without_broker(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_session("demo")
    run = service.create_run(session.session_id, "say done", idempotency_key="queued")
    stored = service.store.get_run(run.run_id)
    assert stored is not None
    assert stored.status == RunStatus.QUEUED
    assert stored.dispatch_pending is True
    assert all(event.type != "model.done" for event in service.store.list_events(run.run_id))


def test_waiting_approval_is_not_claimable_until_decided() -> None:
    from review_agent.harness.task_store import ApprovalRecord, InMemoryTaskStore

    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run_bundle(session.session_id, "patch", idempotency_key="wait")
    store.update_run(run.run_id, status=RunStatus.WAITING_APPROVAL)
    store.create_approval(
        ApprovalRecord(approval_id="intent:p1", run_id=run.run_id, intent={"tool": "apply_patch"}, call_id="p1")
    )
    assert store.claim_run(run.run_id, "w1", lease_seconds=30) is None
    store.set_approval_decision("intent:p1", "allow")
    claimed = store.claim_run(run.run_id, "w1", lease_seconds=30)
    assert claimed is not None
    assert claimed.owner_id == "w1"


UNCHECKABLE_PATCH = (
    "diff --git a/readme.txt b/readme.txt\n"
    "--- a/readme.txt\n"
    "+++ b/readme.txt\n"
    "+fixed\n"
    "-ok\n"
)


def _incomplete_apply(store: InMemoryTaskStore, workspace: Path, patch: str, call_id: str = "p1") -> str:
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "patch")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="apply_patch", arguments={"patch": patch}, call_id=call_id),
        replay_category=ReplayCategory.PATCH_CHECKABLE,
    )
    return run.run_id


def test_uncheckable_patch_unchanged_disk_is_not_applied(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    planned = plan_patch(workspace, UNCHECKABLE_PATCH)
    assert any(not item.checkable for item in planned.files)
    store = InMemoryTaskStore()
    run_id = _incomplete_apply(store, workspace, UNCHECKABLE_PATCH)
    decision = reconcile_run(store=store, workspace_root=workspace, run_id=run_id)
    assert decision.status == "ok"
    record = store.get_tool_execution("p1")
    assert record is not None
    assert record.result is None


def test_uncheckable_patch_diverged_disk_needs_attention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("changed\n", encoding="utf-8")

    def fake_plan(_root: Path, patch: str) -> PatchPlan:
        return PatchPlan(
            patch_hash="deadbeef",
            files=[
                FilePatchPlan(
                    path="readme.txt",
                    before_hash="0" * 64,
                    expected_after_hash=None,
                    is_new=False,
                    is_delete=False,
                    checkable=False,
                    reason="cannot compute expected after hash for this patch shape",
                )
            ],
            patch_text=patch,
        )

    monkeypatch.setattr("review_agent.services.recovery.plan_patch", fake_plan)
    store = InMemoryTaskStore()
    run_id = _incomplete_apply(store, workspace, UNCHECKABLE_PATCH)
    decision = reconcile_run(store=store, workspace_root=workspace, run_id=run_id)
    assert decision.status == "needs_attention"
    assert decision.call_id == "p1"


def test_accept_and_continue_does_not_reblock_same_call(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    manager = WorkspaceManager(settings.review_agent_data_root)
    manager.register("demo", workspace, display_name="Demo")
    model = FakeAsyncModelClient([ModelTurnResult(content="done")])
    service = TaskService(InMemoryTaskStore(), manager, settings, model_client=model)
    session = service.create_session("demo")
    run = service.create_run(session.session_id, "continue", idempotency_key="attn", reviewer="off")
    service.store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="mcp__other__write", arguments={"x": 1}, call_id="m1"),
    )
    decision = reconcile_run(store=service.store, workspace_root=workspace, run_id=run.run_id)
    assert decision.status == "needs_attention"
    assert decision.call_id == "m1"
    mark_needs_attention(service.store, run.run_id, decision)

    service.resume_run(run.run_id, action="accept_and_continue")
    record = service.store.get_tool_execution("m1")
    assert record is not None
    assert record.result is not None
    assert record.execution_status == "succeeded"

    second = reconcile_run(store=service.store, workspace_root=workspace, run_id=run.run_id)
    assert second.status == "ok"

    final = service.run_once(run.run_id)
    assert final.run_status != RunStatus.NEEDS_ATTENTION.value
    stored = service.store.get_run(run.run_id)
    assert stored is not None
    assert stored.status != RunStatus.NEEDS_ATTENTION


def test_repeatable_read_mcp_incomplete_is_skipped(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "mcp")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(
            name="mcp__api_contract__get_endpoint_contract",
            arguments={"contract_id": "items-v1"},
            call_id="c1",
        ),
        replay_category=ReplayCategory.REPEATABLE_READ,
    )
    decision = reconcile_run(store=store, workspace_root=tmp_path, run_id=run.run_id)
    assert decision.status == "ok"
    record = store.get_tool_execution("c1")
    assert record is not None
    assert record.result is None


@pytest.mark.parametrize("execution_status", [None, "executing"])
def test_incomplete_run_command_without_handle_needs_attention(tmp_path: Path, execution_status: str | None) -> None:
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "cmd")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="run_command", arguments={"argv": ["true"]}, call_id="c1"),
        replay_category=ReplayCategory.NON_REPLAYABLE,
    )
    store.complete_tool_execution("c1", None, execution_meta={"status": execution_status})
    decision = reconcile_run(store=store, workspace_root=tmp_path, run_id=run.run_id)
    assert decision.status == "needs_attention"
    assert decision.call_id == "c1"


def test_incomplete_run_command_with_pid_needs_attention(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "cmd")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="run_command", arguments={"argv": ["true"]}, call_id="c1"),
        replay_category=ReplayCategory.NON_REPLAYABLE,
    )
    record = store.get_tool_execution("c1")
    assert record is not None
    record.pid = 4242
    decision = reconcile_run(store=store, workspace_root=tmp_path, run_id=run.run_id)
    assert decision.status == "needs_attention"
    assert decision.call_id == "c1"
    assert "unknown" in decision.reason.lower()



@pytest.mark.parametrize("batch_size", [1, 2])
def test_confirmed_command_resumes_once_after_reassembly(tmp_path: Path, batch_size: int) -> None:
    import sys

    service = _service(tmp_path)
    service.settings = service.settings.model_copy(update={"review_agent_approval_mode": "confirm"})
    model = FakeAsyncModelClient([
        ModelTurnResult(tool_calls=[ToolCall(name="run_command", call_id="approved-command", provider_call_id="approved-command",
            arguments={"argv": [sys.executable, "-c", "from pathlib import Path; p=Path('count'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]}),
            *([ToolCall(name="run_command", call_id="next-command", provider_call_id="next-command", arguments={"argv": [sys.executable, "-c", "from pathlib import Path; Path('second').write_text('done')"]})] if batch_size == 2 else [])]),
        ModelTurnResult(content="done"),
    ])
    service.model_client = model
    run = service.create_run(service.create_session("demo").session_id, "Run command", idempotency_key="command", reviewer="off")
    assert service.run_once(run.run_id).run_status == "waiting_approval"
    approval = service.store.list_approvals(run.run_id)[0]
    service.decide_approval(approval.approval_id, "allow")
    reassembled = TaskService(service.store, service.workspace_manager, service.settings, model_client=model)
    reassembled._memory_checkpointer = service._memory_checkpointer
    result = reassembled.run_once(run.run_id)
    if batch_size == 2:
        assert result.run_status == "waiting_approval"
        pending = next(a for a in service.store.list_approvals(run.run_id) if a.decision is None)
        reassembled.decide_approval(pending.approval_id, "allow")
        result = reassembled.run_once(run.run_id)
    assert result.run_status == "succeeded"
    source = service.workspace_manager.get_run(run.run_id).source_root
    assert (source / "count").read_text() == "x"
    if batch_size == 2:
        assert (source / "second").read_text() == "done"


def test_worker_exception_leaves_run_resumable_and_releases_lease(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    run = service.create_run(service.create_session("demo").session_id, "done", idempotency_key="worker-error", reviewer="off")
    def fail_export(_run_id):
        raise OSError("injected artifact write failure")
    monkeypatch.setattr(service.workspace_manager, "compute_delivery_patch", fail_export)
    with pytest.raises(OSError, match="injected"):
        service.run_once(run.run_id, owner_id="worker-failure")
    current = service.store.get_run(run.run_id)
    assert current.status == RunStatus.INTERRUPTED
    assert current.owner_id is None
    assert any(e.type == "run.interrupted" for e in service.store.list_events(run.run_id))
