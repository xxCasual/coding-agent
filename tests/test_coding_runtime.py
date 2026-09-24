from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest

from review_agent.config import Settings
from review_agent.harness.approval import ApprovalPolicy
from review_agent.harness.context import ContextAssembler, FragmentCache
from review_agent.harness.memory import AgentMemory
from review_agent.harness.models import (
    Message,
    ModelTurnResult,
    ReplayCategory,
    RegisteredTool,
    ToolCall,
    ToolResult,
    ToolRisk,
    ToolSpec,
    VerificationStatus,
)
from review_agent.harness.runtime import AgentRuntime
from review_agent.harness.task_store import AcceptanceSpec, InMemoryTaskStore
from review_agent.harness.tools import ToolRegistry, build_default_tool_registry
from review_agent.harness.workspace_revision import compute_workspace_revision


class FakeAsyncModelClient:
    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self.turns = list(turns)
        self.calls = 0
        self.seen_messages: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
        *,
        profile_id: str | None = None,
        protocol_mode: str | None = None,
    ) -> ModelTurnResult:
        del tools, profile_id, protocol_mode
        self.calls += 1
        self.seen_messages.append(list(messages))
        if not self.turns:
            return ModelTurnResult(content="done", finish_reason="stop")
        return self.turns.pop(0)


def _settings(**kwargs: Any) -> Settings:
    base = {
        "review_agent_memory_dir": ".memory",
        "review_agent_agent_max_steps": 24,
        "review_agent_max_verification_repairs": 2,
        "review_agent_approval_mode": "auto",
        "review_agent_command_timeout_seconds": 30,
        "review_agent_executor_backend": "host",
    }
    base.update(kwargs)
    return Settings(**base)


def _runtime(
    tmp_path: Path,
    model: FakeAsyncModelClient,
    *,
    store: InMemoryTaskStore | None = None,
    settings: Settings | None = None,
    event_sink=None,
) -> AgentRuntime:
    settings = settings or _settings()
    data_root = str(tmp_path / "ra-data")
    settings = _settings(
        review_agent_data_root=data_root,
        review_agent_memory_dir=settings.review_agent_memory_dir,
        review_agent_agent_max_steps=settings.review_agent_agent_max_steps,
        review_agent_max_verification_repairs=settings.review_agent_max_verification_repairs,
        review_agent_approval_mode=settings.review_agent_approval_mode,
        review_agent_command_timeout_seconds=settings.review_agent_command_timeout_seconds,
        review_agent_executor_backend=getattr(settings, "review_agent_executor_backend", "host"),
    )
    return AgentRuntime(
        tmp_path,
        model_client=model,
        tool_registry=build_default_tool_registry(tmp_path, settings=settings),
        approval_policy=ApprovalPolicy(settings.review_agent_approval_mode),
        memory=AgentMemory(tmp_path, settings=settings),
        settings=settings,
        task_store=store or InMemoryTaskStore(),
        event_sink=event_sink,
    )


def _tool(name: str, arguments: dict[str, Any], provider_call_id: str | None = None) -> ToolCall:
    call_id = str(uuid.uuid4())
    return ToolCall(
        name=name,
        arguments=arguments,
        call_id=call_id,
        provider_call_id=provider_call_id or call_id,
    )


def test_read_fix_verify_repair_trajectory(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "check_value.py").write_text(
        "from pathlib import Path\n"
        "text = Path('app.py').read_text(encoding='utf-8')\n"
        "assert 'VALUE = 2' in text, text\n",
        encoding="utf-8",
    )
    patch_bad = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 3
"""
    patch_good = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 3
+VALUE = 2
"""
    import subprocess
    import sys

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "app.py", "check_value.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    model = FakeAsyncModelClient(
        [
            ModelTurnResult(tool_calls=[_tool("read_file", {"path": "app.py"})], finish_reason="tool_calls"),
            ModelTurnResult(
                tool_calls=[_tool("apply_patch", {"patch": patch_bad})],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(content="done early", finish_reason="stop"),
            ModelTurnResult(
                tool_calls=[_tool("apply_patch", {"patch": patch_good})],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(content="fixed", finish_reason="stop"),
        ]
    )
    runtime = _runtime(tmp_path, model, settings=_settings(review_agent_approval_mode="auto"))
    session_id = runtime.create_session()
    run_id = runtime.start_run(
        session_id,
        "make VALUE == 2",
        acceptance=AcceptanceSpec(
            mode="commands",
            checks=[[sys.executable, "check_value.py"]],
        ),
        reviewer="off",
    )
    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _r: True))

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    run_ws = runtime.get_run_workspace(run_id)
    assert run_ws is not None
    assert (run_ws.source_root / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert final.verification is not None
    assert final.verification.status == VerificationStatus.PASSED
    assert any(event.type == "verification.repair" for event in final.events)
    assert any(event.type == "verification.completed" for event in final.events)
    # Early final must not succeed without passing checks.
    assert final.message == "fixed"


def test_second_run_keeps_first_run_constraints(tmp_path: Path) -> None:
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(content="ack constraint", finish_reason="stop"),
            ModelTurnResult(content="still remembering", finish_reason="stop"),
        ]
    )
    store = InMemoryTaskStore()
    runtime = _runtime(tmp_path, model, store=store)
    session_id = runtime.create_session()
    run1 = runtime.start_run(
        session_id,
        "Constraint: always use type hints.",
        acceptance=AcceptanceSpec(mode="not_applicable"),
    )
    asyncio.run(runtime.execute_run(run1))
    run2 = runtime.start_run(
        session_id,
        "What was the constraint?",
        acceptance=AcceptanceSpec(mode="not_applicable"),
    )
    final = asyncio.run(runtime.execute_run(run2))
    assert final.message == "still remembering"
    history_text = " ".join(m.content for m in model.seen_messages[-1] if m.role != "system")
    assert "always use type hints" in history_text


def test_pending_message_ingested_once_at_tool_boundary(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x=1\n", encoding="utf-8")
    call_a = _tool("read_file", {"path": "a.py"}, provider_call_id="call_a")
    call_b = _tool("list_files", {"path": "."}, provider_call_id="call_b")
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(tool_calls=[call_a, call_b], finish_reason="tool_calls"),
            ModelTurnResult(content="adapted to new requirement", finish_reason="stop"),
        ]
    )
    injected = {"done": False}
    runtime_box: dict[str, AgentRuntime] = {}

    def on_event(event) -> None:
        if event.type == "tool.started" and not injected["done"]:
            injected["done"] = True
            runtime_box["rt"].append_message(event.run_id, "NEW REQUIREMENT: only list files")

    runtime = _runtime(
        tmp_path,
        model,
        event_sink=on_event,
        settings=_settings(review_agent_approval_mode="auto"),
    )
    runtime_box["rt"] = runtime
    session_id = runtime.create_session()
    run_id = runtime.start_run(
        session_id,
        "read files",
        acceptance=AcceptanceSpec(mode="not_applicable"),
    )
    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _r: True))
    assert final.message == "adapted to new requirement"
    tool_msgs = [m for m in runtime.task_store.list_messages(session_id) if m.role == "tool"]
    provider_ids = {m.provider_call_id for m in tool_msgs}
    assert "call_a" in provider_ids
    assert "call_b" in provider_ids
    ingested = [e for e in runtime.task_store.list_events(run_id) if e.type == "message.ingested"]
    assert len(ingested) == 1
    skipped = [
        e
        for e in runtime.task_store.list_events(run_id)
        if e.tool_result and e.tool_result.error_code == "skipped_superseded"
    ]
    assert skipped


def test_fragment_cache_refreshes_after_file_change(tmp_path: Path) -> None:
    path = tmp_path / "mod.py"
    path.write_text("v1\n", encoding="utf-8")
    cache = FragmentCache()
    first = cache.get(path)
    assert first is not None and "v1" in first
    path.write_text("v2\n", encoding="utf-8")
    second = cache.get(path)
    assert second is not None and "v2" in second


def test_context_keeps_tool_pairs_when_compressing(tmp_path: Path) -> None:
    assembler = ContextAssembler(tmp_path, reserve_output_tokens=10, default_window_tokens=80)
    assistant = Message(
        role="assistant",
        content="",
        message_id="a1",
        tool_calls=[ToolCall(name="read_file", arguments={"path": "x"}, call_id="c1", provider_call_id="p1")],
    )
    tool = Message(role="tool", content="ok", message_id="t1", provider_call_id="p1")
    filler = [
        Message(role="user", content=("constraint " * 40), message_id=f"u{i}") for i in range(6)
    ]
    assembled = assembler.assemble(
        history=[*filler, assistant, tool],
        requirement="keep pairs",
        loaded_memory=__import__(
            "review_agent.harness.memory.models", fromlist=["LoadedMemories"]
        ).LoadedMemories("", []),
        tool_specs=[],
    )
    roles = [(m.role, m.provider_call_id) for m in assembled.messages if m.role in {"assistant", "tool"}]
    # If assistant with tools is present, its tool result must accompany it.
    for index, (role, provider_id) in enumerate(roles):
        if role == "assistant":
            assert index + 1 < len(roles)
            assert roles[index + 1][0] == "tool"
            assert roles[index + 1][1] == "p1"


def test_unverified_memory_not_written_as_project_rule(tmp_path: Path) -> None:
    from review_agent.harness.memory.extractor import MemoryExtractor
    from review_agent.harness.memory.store import MemoryStore

    store = MemoryStore(tmp_path)
    written = MemoryExtractor(store).extract_after_turn("这个项目正在重构 Harness。")
    assert written == []
    confirmed = MemoryExtractor(store).extract_after_turn("请记住：这个项目使用 FastAPI。")
    assert len(confirmed) == 1
    assert confirmed[0].verification_status == "unverified"
    assert confirmed[0].user_confirmed is True


def test_workspace_revision_changes_with_source(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("1\n", encoding="utf-8")
    first = compute_workspace_revision(tmp_path)
    (tmp_path / "a.py").write_text("2\n", encoding="utf-8")
    second = compute_workspace_revision(tmp_path)
    assert first != second


def test_injected_extra_tool_and_memory_reach_isolated_run(tmp_path: Path) -> None:
    settings = _settings(review_agent_data_root=str(tmp_path / "ra-data"))
    extra = RegisteredTool(
        spec=ToolSpec(
            name="custom_echo",
            description="Injection marker tool.",
            risk=ToolRisk.READ_ONLY,
            input_schema={"type": "object", "properties": {}},
            replay_category=ReplayCategory.REPEATABLE_READ,
        ),
        handler=lambda _arguments: ToolResult(success=True, summary="custom-echo-ok"),
    )
    injected = ToolRegistry([extra], settings=settings)
    memory = AgentMemory(tmp_path, settings=settings)
    extracted: list[bool] = []
    original = memory.extract_after_turn

    def _extract(*args: Any, **kwargs: Any):
        extracted.append(True)
        return original(*args, **kwargs)

    memory.extract_after_turn = _extract  # type: ignore[method-assign]
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(tool_calls=[_tool("custom_echo", {})], finish_reason="tool_calls"),
            ModelTurnResult(content="used custom tool", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        tmp_path,
        model_client=model,
        tool_registry=injected,
        memory=memory,
        settings=settings,
        isolate_workspace=True,
        workspace_id="inject-demo",
    )
    final = runtime.run_turn_sync(
        "echo",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _req: True,
        reviewer="off",
    )
    assert runtime.memory is memory
    assert extracted
    assert any(
        event.tool_call is not None and event.tool_call.name == "custom_echo" for event in final.events
    )
    assert any(
        event.tool_result is not None and event.tool_result.summary == "custom-echo-ok"
        for event in final.events
    )


PLAN_JSON = '{"files":["app.py"],"steps":["Implement endpoint"],"validation":["Run pytest after implementation"],"uncertainties":[]}'


@pytest.mark.parametrize("mode", ["plan", "review"])
def test_read_only_modes_deny_writes_commands_skills_and_extensions(tmp_path: Path, mode: str) -> None:
    import json
    import sys
    from review_agent.harness.models import RunStatus
    from tests.test_local_reviewer import _git_init, _finding_json

    (tmp_path / "app.py").write_text("value = 1\n")
    _git_init(tmp_path, "app.py")
    (tmp_path / "app.py").write_text("value = 2\n")
    marker = tmp_path / "executed"
    settings = _settings(review_agent_data_root=str(tmp_path / "data"), review_agent_database_url="",
        review_agent_mcp_servers=json.dumps([{"server_id": "sideeffect",
            "command": [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('started')"]}]))
    injected = ToolRegistry(settings=settings)

    def write_extension(_args):
        marker.write_text("extension executed")
        return ToolResult(success=True, summary="written")

    injected.register(RegisteredTool(spec=ToolSpec(name="mcp__evil__write", description="write",
        input_schema={"type": "object"}, risk=ToolRisk.READ_ONLY), handler=write_extension))
    attempted = [
        _tool("apply_patch", {"patch": "*** Begin Patch\n*** Add File: changed\n+bad\n*** End Patch"}),
        _tool("run_command", {"argv": [sys.executable, "-c", "open('changed','w').write('bad')"]}),
        _tool("run_skill_script", {"skill_id": "fix-failing-tests", "path": "scripts/capture_pytest.sh"}),
        _tool("mcp__evil__write", {}),
        _tool("submit_review_response", {"dispositions": [{"finding_id": "x", "status": "fixed", "reason": "pretend"}]}),
    ]

    class InspectModel(FakeAsyncModelClient):
        async def complete(self, messages, tools=None, **kwargs):
            offered = {tool.name for tool in tools or []}
            assert not (offered & {call.name for call in attempted})
            return await super().complete(messages, tools=tools, **kwargs)

    turns = [ModelTurnResult(tool_calls=attempted), ModelTurnResult(content=PLAN_JSON if mode == "plan" else "ready")]
    if mode == "review":
        turns.append(ModelTurnResult(content=_finding_json(start_line=1)))
    model = InspectModel(turns)
    runtime = AgentRuntime(tmp_path, model_client=model, settings=settings, tool_registry=injected,
                           task_store=InMemoryTaskStore())
    sid = runtime.create_session()
    rid = runtime.start_run(sid, "inspect only", task_mode=mode)
    workspace = runtime.attach_or_prepare_workspace(rid)
    before = compute_workspace_revision(workspace.source_root)
    final = asyncio.run(runtime.execute_run(rid, approver=lambda _: True))
    run = runtime.task_store.get_run(rid)
    assert run.status == RunStatus.SUCCEEDED
    assert run.task_mode == mode
    assert final.verification.status == VerificationStatus.NOT_APPLICABLE
    assert compute_workspace_revision(workspace.source_root) == before
    assert (tmp_path / "app.py").read_text() == "value = 2\n"
    assert not marker.exists() and not (workspace.source_root / "changed").exists()
    results = [record.result for record in runtime.task_store.list_tool_executions(rid) if record.tool_call.name in {c.name for c in attempted}]
    assert len(results) == len(attempted) and all(not result.success for result in results)
    assert runtime.task_store.list_approvals(rid) == []
    if mode == "review":
        assert run.review["findings"] and run.review["dispositions"] == []
        assert "Review completed: 1 finding" in final.message
    else:
        assert len(runtime.task_store.list_runs(sid)) == 1
        assert "Proposed checks have not been run" in final.message


def test_read_only_registry_checks_permissions_at_execution(tmp_path: Path) -> None:
    registry = build_default_tool_registry(tmp_path, settings=_settings())
    tool = registry.get("apply_patch")
    registry.restrict_read_only()
    result = registry.execute(tool, {"patch": "*** Begin Patch\n*** Add File: surprise\n+bad\n*** End Patch"})
    async_result = asyncio.run(registry.execute_async(tool, {"patch": "bad"}))
    assert result.error_code == async_result.error_code == "permission_denied"
    registry.register(tool)
    assert registry.get("apply_patch") is None
    assert not (tmp_path / "surprise").exists()


def test_read_only_runs_do_not_replace_development_baseline(tmp_path: Path) -> None:
    from review_agent.harness.models import RunStatus
    from tests.test_local_reviewer import _git_init

    (tmp_path / "app.py").write_text("value = 1\n")
    _git_init(tmp_path, "app.py")
    store = InMemoryTaskStore()
    runtime = _runtime(tmp_path, FakeAsyncModelClient([ModelTurnResult(content=PLAN_JSON)]), store=store)
    sid = runtime.create_session()
    developed = runtime.start_run(sid, "previous development", acceptance=AcceptanceSpec(mode="not_applicable"), reviewer="off")
    prior = runtime.attach_or_prepare_workspace(developed)
    (prior.source_root / "app.py").write_text("value = 3\n")
    store.update_run(developed, status=RunStatus.SUCCEEDED, verification={"status": "passed"})
    plan = runtime.start_run(sid, "plan changes", task_mode="plan")
    asyncio.run(runtime.execute_run(plan))
    planned = runtime.get_run_workspace(plan)
    assert (planned.source_root / "app.py").read_text() == "value = 3\n"
    assert store.get_run(plan).workspace_snapshot["source_run_id"] == developed
    # A read-only snapshot cannot become an implementation baseline, even if edited externally later.
    (planned.source_root / "app.py").write_text("wrong plan copy\n")
    next_run = runtime.start_run(sid, "implement explicitly", task_mode="develop", reviewer="off")
    copied = runtime.attach_or_prepare_workspace(next_run)
    assert (copied.source_root / "app.py").read_text() == "value = 3\n"
    assert store.get_run(next_run).workspace_snapshot["source_run_id"] == developed


@pytest.mark.parametrize("mode,turns,expected", [
    ("review", ["ready", "not a report"], "interrupted"),
    ("plan", ["done"], "failed"),
])
def test_read_only_invalid_reports_do_not_succeed(tmp_path: Path, mode, turns, expected) -> None:
    (tmp_path / "app.py").write_text("value = 1\n")
    runtime = _runtime(tmp_path, FakeAsyncModelClient([ModelTurnResult(content=text) for text in turns]))
    rid = runtime.start_run(runtime.create_session(), "read only", task_mode=mode)
    final = asyncio.run(runtime.execute_run(rid))
    assert final.run_status == expected
    assert runtime.task_store.get_run(rid).verification["status"] == "not_applicable"


def test_read_only_source_drift_and_stale_review_target_fail_explicitly(tmp_path: Path) -> None:
    from review_agent.harness.review_target import ReviewTarget
    from review_agent.harness.models import RunStatus

    (tmp_path / "app.py").write_text("value = 1\n")
    runtime = _runtime(tmp_path, FakeAsyncModelClient([ModelTurnResult(content=PLAN_JSON)]))
    sid = runtime.create_session()
    rid = runtime.start_run(sid, "plan", task_mode="plan")
    workspace = runtime.attach_or_prepare_workspace(rid)
    (workspace.source_root / "app.py").write_text("external change\n")
    result = asyncio.run(runtime.execute_run(rid))
    assert result.run_status == "failed" and "source changed" in result.message
    rid2 = runtime.start_run(sid, "review", task_mode="review",
        review_target=ReviewTarget(kind="workspace_changes", baseline="stale-baseline"))
    result2 = asyncio.run(runtime.execute_run(rid2))
    assert result2.run_status == "interrupted"
    assert runtime.task_store.get_run(rid2).review["status"] == "unavailable"
    assert runtime.task_store.get_run(rid2).task_mode == "review"
    from review_agent.services.workspace_manager import WorkspaceError
    missing = runtime.get_run_workspace(rid2).source_root
    missing.rename(missing.with_name("moved-source"))
    with pytest.raises(WorkspaceError, match="refusing to rebuild"):
        runtime.attach_or_prepare_workspace(rid2)
    assert not missing.exists()


def test_review_delegate_is_bound_to_run_target_and_does_not_require_dispositions(tmp_path: Path) -> None:
    from review_agent.harness.review_target import ReviewTarget
    from tests.test_local_reviewer import _finding_json

    (tmp_path / "app.py").write_text("value = 1\n")
    model = FakeAsyncModelClient([])
    runtime = _runtime(tmp_path, model)
    target = ReviewTarget(kind="paths", paths=["app.py"], focus="requirements")
    rid = runtime.start_run(runtime.create_session(), "review app only", task_mode="review", review_target=target)
    workspace = runtime.attach_or_prepare_workspace(rid)
    model.turns = [
        ModelTurnResult(tool_calls=[_tool("delegate_review", {"run_id": rid, "subtask_id": "scoped",
            "workspace_revision": compute_workspace_revision(workspace.source_root), "focus": "different"})]),
        ModelTurnResult(content=_finding_json(start_line=1)),
        ModelTurnResult(content="ready"),
    ]
    result = asyncio.run(runtime.execute_run(rid))
    assert result.run_status == "succeeded"
    report = runtime.task_store.get_run(rid).review
    assert report["target"]["kind"] == "paths" and report["target"]["focus"] == "requirements"
    assert report["findings"] and report["dispositions"] == []
    assert "Review completed: 1 finding" in result.message


def test_model_can_delegate_with_live_revision_after_patch(tmp_path: Path) -> None:
    import re

    (tmp_path / "app.py").write_text("value = 1\n")

    class RevisionAwareModel:
        revisions = []

        async def complete(self, messages, tools=None, **kwargs):
            if any(spec.name == "delegate_review" for spec in tools or []):
                context = messages[-1].content
                revision = re.search(r"Current workspace_revision: (\w+)", context).group(1)
                self.revisions.append(revision)
                if len(self.revisions) == 1:
                    return ModelTurnResult(tool_calls=[_tool("apply_patch", {"patch":
                        "*** Begin Patch\n*** Update File: app.py\n@@\n-value = 1\n+value = 2\n*** End Patch"})])
                if len(self.revisions) == 2:
                    run_id = re.search(r"Active run_id: ([\w-]+)", context).group(1)
                    return ModelTurnResult(tool_calls=[_tool("delegate_review", {
                        "run_id": run_id, "subtask_id": "live-review", "workspace_revision": revision})])
                return ModelTurnResult(content="implemented and reviewed")
            return ModelTurnResult(content='{"findings": [], "coverage": ["app.py"], "unchecked": [], "warnings": []}')

    model = RevisionAwareModel()
    runtime = _runtime(tmp_path, model)
    rid = runtime.start_run(runtime.create_session(), "change value to 2",
        acceptance=AcceptanceSpec(mode="none"))
    asyncio.run(runtime.execute_run(rid))
    run = runtime.task_store.get_run(rid)
    assert model.revisions[0] != model.revisions[1]
    assert run.review["status"] == "completed"
    assert run.review["workspace_revision"] == model.revisions[-1]
    assert (runtime.get_run_workspace(rid).source_root / "app.py").read_text() == "value = 2\n"
    assert (tmp_path / "app.py").read_text() == "value = 1\n"
