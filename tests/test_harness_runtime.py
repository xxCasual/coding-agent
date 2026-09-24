from pathlib import Path
from typing import Any
from unittest.mock import patch
import uuid

import pytest

from review_agent.config import Settings
from review_agent.harness.approval import ApprovalPolicy
from review_agent.harness.memory import AgentMemory
from review_agent.harness.models import ModelTurnResult, ToolCall
from review_agent.harness.runtime import AgentRuntime
from review_agent.harness.task_store import AcceptanceSpec
from review_agent.harness.tools import build_default_tool_registry
from review_agent.tools.verification_tools import pytest_targeted_tool, ruff_check_tool


class FakeModelClient:
    """Async Fake matching OpenAICompatibleModelClient.complete."""

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self.actions = actions
        self.calls = 0

    async def complete(self, messages, tools=None, *, profile_id=None, protocol_mode=None):
        del messages, tools, profile_id, protocol_mode
        self.calls += 1
        if not self.actions:
            return ModelTurnResult(content="done", finish_reason="stop")
        action = self.actions.pop(0)
        if action.get("type") == "final":
            return ModelTurnResult(content=str(action.get("message", "")), finish_reason="stop")
        name = str(action.get("tool", ""))
        arguments = dict(action.get("input") or action.get("arguments") or {})
        call_id = str(uuid.uuid4())
        return ModelTurnResult(
            tool_calls=[
                ToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    provider_call_id=call_id,
                )
            ],
            finish_reason="tool_calls",
        )


def _runtime(tmp_path: Path, model: FakeModelClient, max_steps: int = 12) -> AgentRuntime:
    settings = Settings(
        review_agent_memory_dir=".memory",
        review_agent_agent_max_steps=max_steps,
        review_agent_approval_mode="confirm",
        review_agent_command_timeout_seconds=30,
        review_agent_data_root=str(tmp_path / "ra-data"),
        review_agent_executor_backend="host",
    )
    return AgentRuntime(
        tmp_path,
        model_client=model,
        tool_registry=build_default_tool_registry(tmp_path, settings=settings),
        approval_policy=ApprovalPolicy("confirm"),
        memory=AgentMemory(tmp_path, settings=settings),
        settings=settings,
    )


def _tool_result_events(final):
    return [event for event in final.events if event.tool_result is not None]


def test_runtime_executes_tool_and_returns_final_answer(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    runtime = _runtime(
        tmp_path,
        FakeModelClient(
            [
                {"type": "tool", "tool": "list_files", "input": {"path": "."}},
                {"type": "final", "message": "I found app.py."},
            ]
        ),
    )

    final = runtime.run_turn_sync(
        "list files",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _r: True,
    )

    assert final.message == "I found app.py."
    assert any(event.type == "tool.finished" for event in final.events)
    memory_root = runtime.workspace_manager.memory_root_for(runtime.workspace_id)
    assert (memory_root / "MEMORY.md").exists() or (tmp_path / ".memory" / "MEMORY.md").exists()
    finished = next(event for event in final.events if event.type == "tool.finished")
    assert finished.tool_call is not None
    assert finished.tool_call.call_id
    assert finished.tool_result is not None
    assert finished.tool_result.call_id == finished.tool_call.call_id


def test_runtime_blocks_dangerous_command_without_execution(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        FakeModelClient(
            [
                {"type": "tool", "tool": "run_command", "input": {"command": ["sudo", "echo", "no"]}},
                {"type": "final", "message": "blocked"},
            ]
        ),
    )

    final = runtime.run_turn_sync(
        "run dangerous command",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _r: True,
    )

    assert final.message == "blocked"
    assert any(event.type == "approval.blocked" for event in final.events)


def test_runtime_rejects_write_tool_when_approval_denied(tmp_path: Path) -> None:
    patch = """diff --git a/new.txt b/new.txt
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+hello
"""
    runtime = _runtime(
        tmp_path,
        FakeModelClient(
            [
                {"type": "tool", "tool": "apply_patch", "input": {"patch": patch}},
                {"type": "final", "message": "not applied"},
            ]
        ),
    )

    final = runtime.run_turn_sync(
        "apply this patch",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _request: False,
    )

    assert final.message == "not applied"
    assert not (tmp_path / "new.txt").exists()
    assert any(event.type == "approval.rejected" for event in final.events)


def test_runtime_reports_workspace_boundary_violation(tmp_path: Path) -> None:
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("secret\n", encoding="utf-8")
    runtime = _runtime(
        tmp_path,
        FakeModelClient(
            [
                {"type": "tool", "tool": "read_file", "input": {"path": "../secret.txt"}},
                {"type": "final", "message": "done"},
            ]
        ),
    )

    final = runtime.run_turn_sync(
        "read outside file",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _r: True,
    )

    failed = [event for event in final.events if event.tool_result and not event.tool_result.success]
    assert failed
    assert "workspace" in failed[0].tool_result.summary.lower() or "outside" in failed[0].tool_result.summary.lower() or "denied" in failed[0].tool_result.summary.lower() or "path" in failed[0].tool_result.summary.lower()


def test_runtime_stops_at_max_steps(tmp_path: Path) -> None:
    runtime = _runtime(
        tmp_path,
        FakeModelClient(
            [
                {"type": "tool", "tool": "list_files", "input": {"path": "."}},
                {"type": "tool", "tool": "list_files", "input": {"path": "."}},
            ]
        ),
        max_steps=1,
    )

    final = runtime.run_turn_sync(
        "keep going",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _r: True,
    )

    assert "max agent steps" in final.message.lower()


@pytest.mark.parametrize(
    ("tool", "arguments", "marker"),
    [
        ("read_file", {}, "path"),
        ("apply_patch", {"patch": "   "}, "patch"),
        ("run_command", {"argv": []}, "argv"),
        ("list_files", {"path": ".", "limit": 0}, "limit"),
        ("list_files", {"path": ".", "unknown": 1}, "unknown"),
    ],
)
def test_invalid_tool_arguments_rejected_without_side_effects(
    tmp_path: Path,
    tool: str,
    arguments: dict[str, Any],
    marker: str,
) -> None:
    sentinel = tmp_path / "untouched.txt"
    sentinel.write_text("keep\n", encoding="utf-8")
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    runtime = _runtime(
        tmp_path,
        FakeModelClient(
            [
                {"type": "tool", "tool": tool, "input": arguments},
                {"type": "final", "message": "rejected"},
            ]
        ),
    )

    final = runtime.run_turn_sync(
        "bad args",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _r: True,
    )

    failed = [event for event in _tool_result_events(final) if event.type == "tool.failed"]
    assert failed
    assert failed[0].tool_result is not None
    assert failed[0].tool_result.error_code == "invalid_arguments"
    assert marker in failed[0].tool_result.summary
    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    before_workspace = {
        key: value
        for key, value in before.items()
        if not key.startswith(".memory") and not key.startswith("ra-data")
    }
    after_workspace = {
        key: value
        for key, value in after.items()
        if not key.startswith(".memory") and not key.startswith("ra-data")
    }
    assert after_workspace == before_workspace


def test_valid_read_search_and_patch(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    patch = """diff --git a/note.txt b/note.txt
new file mode 100644
--- /dev/null
+++ b/note.txt
@@ -0,0 +1 @@
+patched
"""
    runtime = _runtime(
        tmp_path,
        FakeModelClient(
            [
                {"type": "tool", "tool": "read_file", "input": {"path": "app.py"}},
                {"type": "tool", "tool": "search_text", "input": {"query": "hello"}},
                {"type": "tool", "tool": "apply_patch", "input": {"patch": patch}},
                {"type": "final", "message": "done"},
            ]
        ),
    )

    final = runtime.run_turn_sync(
        "read search patch",
        acceptance=AcceptanceSpec(mode="not_applicable"),
        approver=lambda _request: True,
    )

    finished = [event for event in final.events if event.type == "tool.finished"]
    assert len(finished) == 3
    assert all(event.tool_result and event.tool_result.success for event in finished)
    assert not (tmp_path / "note.txt").exists()
    run_ws = runtime.get_run_workspace(final.run_id)
    assert run_ws is not None
    assert (run_ws.source_root / "note.txt").read_text(encoding="utf-8") == "patched\n"


def test_verification_skip_is_unverified_not_passed(tmp_path: Path) -> None:
    empty_pytest = pytest_targeted_tool(tmp_path, [])
    assert empty_pytest.verification_status == "unverified"
    assert empty_pytest.success is False

    with patch("review_agent.tools.verification_tools.shutil.which", return_value=None):
        missing_ruff = ruff_check_tool(tmp_path, ["app.py"])
    assert missing_ruff.verification_status == "unverified"
    assert missing_ruff.success is False


def test_tool_specs_expose_non_empty_schemas(tmp_path: Path) -> None:
    registry = build_default_tool_registry(tmp_path)
    by_name = {spec.name: spec for spec in registry.specs()}
    assert by_name["run_command"].input_schema["properties"]["argv"]["type"] == "array"
    assert "command" not in by_name["run_command"].input_schema.get("properties", {})
    assert "path" in by_name["read_file"].input_schema.get("required", [])
