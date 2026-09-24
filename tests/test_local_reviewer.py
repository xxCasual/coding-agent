from __future__ import annotations

import asyncio
import difflib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from review_agent.config import Settings
from review_agent.harness.approval import ApprovalPolicy
from review_agent.harness.memory import AgentMemory
from review_agent.harness.models import ModelTurnResult, RunStatus, ToolCall, ToolResult, VerificationStatus
from review_agent.harness.reviewer import LocalReviewService, current_patch, forced_review_call_id
from review_agent.harness.runtime import AgentRuntime
from review_agent.harness.task_store import AcceptanceSpec, InMemoryTaskStore
from review_agent.harness.tools import build_reviewer_tool_registry
from review_agent.harness.workspace_revision import compute_workspace_revision
from tests.test_coding_runtime import FakeAsyncModelClient, _tool


INCOMPLETE_APP = """from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


class ItemCreate(BaseModel):
    name: str = Field(min_length=1)
    quantity: int = Field(ge=1)


class Item(BaseModel):
    id: str
    name: str
    quantity: int


@app.post("/items", response_model=Item, status_code=201)
def create_item(payload: ItemCreate) -> Item:
    return Item(id="item-1", name=payload.name, quantity=payload.quantity)
"""

FIXED_APP = """from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()


class ItemCreate(BaseModel):
    name: str = Field(min_length=1)
    quantity: int = Field(ge=1)


class Item(BaseModel):
    id: str
    name: str
    quantity: int


@app.post("/items", response_model=Item, status_code=201)
def create_item(payload: ItemCreate) -> Item:
    if payload.quantity > 1000:
        raise HTTPException(status_code=400, detail="quantity too large")
    return Item(id="item-1", name=payload.name, quantity=payload.quantity)
"""

STUB_APP = "from fastapi import FastAPI\napp = FastAPI()\n"

CHECK_SCRIPT = (
    "from pathlib import Path\n"
    "text = Path('app.py').read_text(encoding='utf-8')\n"
    "assert 'create_item' in text\n"
)


def _settings(tmp_path: Path, **kwargs: Any) -> Settings:
    base = {
        "review_agent_memory_dir": ".memory",
        "review_agent_agent_max_steps": 24,
        "review_agent_reviewer_max_steps": 6,
        "review_agent_max_verification_repairs": 2,
        "review_agent_approval_mode": "auto",
        "review_agent_command_timeout_seconds": 30,
        "review_agent_executor_backend": "host",
        "review_agent_data_root": str(tmp_path / "ra-data"),
    }
    base.update(kwargs)
    return Settings(**base)


def _git_init(root: Path, *files: str) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    if files:
        subprocess.run(["git", "add", *files], cwd=root, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-m", "init"],
            cwd=root,
            check=True,
            capture_output=True,
        )


def _runtime(tmp_path: Path, model: FakeAsyncModelClient, *, settings: Settings | None = None) -> AgentRuntime:
    settings = settings or _settings(tmp_path)
    return AgentRuntime(
        tmp_path,
        model_client=model,
        approval_policy=ApprovalPolicy("auto"),
        memory=AgentMemory(tmp_path, settings=settings),
        settings=settings,
        task_store=InMemoryTaskStore(),
    )


def _patch(old: str, new: str, path: str = "app.py") -> str:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    if old_lines and not old_lines[-1].endswith("\n"):
        old_lines[-1] += "\n"
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    body = "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{path}", tofile=f"b/{path}")
    )
    return f"diff --git a/{path} b/{path}\n{body}"


def _finding_json(*, file_path: str = "app.py", start_line: int = 18) -> str:
    return json.dumps(
        {
            "findings": [
                {
                    "finding_id": "F-missing-error-branch",
                    "hunk_id": f"{file_path}:{start_line}:0",
                    "file_path": file_path,
                    "start_line": start_line,
                    "end_line": start_line,
                    "severity": "high",
                    "category": "bug",
                    "title": "Missing exception/error response branch",
                    "evidence": "create_item returns 201 only; no HTTPException for invalid quantity",
                    "explanation": "The local interface diff omits an error response path.",
                    "suggestion": "Raise HTTPException(status_code=400) for invalid quantity.",
                    "confidence": 0.92,
                    "is_blocking": True,
                }
            ]
        }
    )


def test_reviewer_collaboration_finds_missing_error_branch(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(STUB_APP, encoding="utf-8")
    (tmp_path / "check_app.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    _git_init(tmp_path, "app.py", "check_app.py")
    incomplete = _patch(STUB_APP, INCOMPLETE_APP)
    fixed = _patch(INCOMPLETE_APP, FIXED_APP)
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(tool_calls=[_tool("apply_patch", {"patch": incomplete})], finish_reason="tool_calls"),
            ModelTurnResult(content="ready for review", finish_reason="stop"),
            ModelTurnResult(tool_calls=[_tool("read_file", {"path": "app.py"})], finish_reason="tool_calls"),
            ModelTurnResult(content=_finding_json(), finish_reason="stop"),
            ModelTurnResult(
                tool_calls=[
                    _tool("apply_patch", {"patch": fixed}),
                    _tool(
                        "submit_review_response",
                        {
                            "dispositions": [
                                {
                                    "finding_id": "F-missing-error-branch",
                                    "status": "fixed",
                                    "reason": "Added HTTPException 400 branch",
                                }
                            ]
                        },
                    ),
                ],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(content="fixed after review", finish_reason="stop"),
            ModelTurnResult(content='{"findings":[]}', finish_reason="stop"),
        ]
    )
    runtime = _runtime(tmp_path, model)
    session_id = runtime.create_session()
    run_id = runtime.start_run(
        session_id,
        "Implement POST /items with an error response branch for invalid quantity. No PR URL.",
        acceptance=AcceptanceSpec(mode="commands", checks=[[sys.executable, "check_app.py"]]),
    )
    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _r: True))
    run = runtime.task_store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.SUCCEEDED
    assert final.verification is not None
    assert final.verification.status == VerificationStatus.PASSED
    assert run.review is not None
    assert run.review["status"] == "completed"
    events = runtime.task_store.list_events(run_id)
    completed = [event for event in events if event.type == "delegate.completed"]
    assert any(int(event.payload.get("findings_count") or 0) > 0 for event in completed)
    assert any(event.type == "review.disposition" for event in events)
    ws = runtime.get_run_workspace(run_id)
    assert ws is not None
    assert "HTTPException" in (ws.source_root / "app.py").read_text(encoding="utf-8")
    assert any(event.type == "delegate.started" for event in events)

    reviewer_rounds = [
        msgs
        for msgs in model.seen_messages
        if any(m.role == "system" and "read-only code reviewer" in (m.content or "") for m in msgs)
    ]
    assert reviewer_rounds
    for msgs in reviewer_rounds:
        for message in msgs:
            names = [call.name for call in (message.tool_calls or [])]
            assert "apply_patch" not in names
            assert "delegate_review" not in names
            assert not str(message.message_id).startswith("reviewer-tool:") or message.role == "tool"
    parent_messages = runtime.task_store.list_messages(session_id)
    assert not any(str(item.message_id).startswith("reviewer-tool:") for item in parent_messages)
    assert not any(item.role == "system" and "read-only code reviewer" in item.content for item in parent_messages)


def test_reviewer_cannot_call_write_tools(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(INCOMPLETE_APP, encoding="utf-8")
    _git_init(tmp_path, "app.py")
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(
                tool_calls=[_tool("apply_patch", {"patch": _patch(INCOMPLETE_APP, FIXED_APP)})],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(content='{"findings":[]}', finish_reason="stop"),
        ]
    )
    store = InMemoryTaskStore()
    session = store.create_session("ws")
    run = store.create_run(session.session_id, "review")
    before = (tmp_path / "app.py").read_text(encoding="utf-8")
    result = asyncio.run(
        LocalReviewService(
            tmp_path,
            store=store,
            model_complete=model.complete,
            settings=_settings(tmp_path),
        ).run(
            run_id=run.run_id,
            subtask_id="r1",
            workspace_revision=compute_workspace_revision(tmp_path),
            requirement="find issues",
            acceptance_text="mode=commands",
            patch=current_patch(tmp_path),
            verification_text="",
            contract_evidence=[],
        )
    )
    assert result.status == "completed"
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == before
    denied = build_reviewer_tool_registry(tmp_path, settings=_settings(tmp_path)).validate_arguments(
        "apply_patch", {"patch": "x"}, call_id="x"
    )
    assert isinstance(denied, ToolResult)
    assert denied.error_code == "unknown_tool"
    assistant = next(m for msgs in model.seen_messages for m in msgs if m.tool_calls)
    assert assistant.tool_calls[0].name == "apply_patch"


def test_stale_revision_and_budget_do_not_look_like_passed(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(INCOMPLETE_APP, encoding="utf-8")
    _git_init(tmp_path, "app.py")
    store = InMemoryTaskStore()
    session = store.create_session("ws")
    run = store.create_run(session.session_id, "review")
    stale = asyncio.run(
        LocalReviewService(
            tmp_path,
            store=store,
            model_complete=FakeAsyncModelClient([]).complete,
            settings=_settings(tmp_path),
        ).run(
            run_id=run.run_id,
            subtask_id="r-stale",
            workspace_revision="not-the-current-hash",
            requirement="review",
            acceptance_text="",
            patch="",
            verification_text="",
            contract_evidence=[],
        )
    )
    assert stale.status == "failed"
    assert stale.findings == []
    assert any("stale" in item.lower() for item in stale.warnings)

    limited = store.create_run(store.create_session("ws2").session_id, "budget")
    store.update_run(
        limited.run_id,
        budget={"max_steps": 24, "step_count": 24, "reviewer_max_steps": 6, "reviewer_step_count": 0},
    )
    exhausted = asyncio.run(
        LocalReviewService(
            tmp_path,
            store=store,
            model_complete=FakeAsyncModelClient(
                [ModelTurnResult(content='{"findings":[]}', finish_reason="stop")]
            ).complete,
            settings=_settings(tmp_path),
        ).run(
            run_id=limited.run_id,
            subtask_id="r-budget",
            workspace_revision=compute_workspace_revision(tmp_path),
            requirement="review",
            acceptance_text="",
            patch="",
            verification_text="",
            contract_evidence=[],
        )
    )
    assert exhausted.status == "interrupted"
    assert exhausted.findings == []
    assert exhausted.warnings


def test_invalid_reviewer_json_is_unavailable_not_clean(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(INCOMPLETE_APP, encoding="utf-8")
    _git_init(tmp_path, "app.py")
    store = InMemoryTaskStore()
    session = store.create_session("ws")
    run = store.create_run(session.session_id, "review")
    result = asyncio.run(
        LocalReviewService(
            tmp_path,
            store=store,
            model_complete=FakeAsyncModelClient([ModelTurnResult(content="done", finish_reason="stop")]).complete,
            settings=_settings(tmp_path),
        ).run(
            run_id=run.run_id,
            subtask_id="r-bad",
            workspace_revision=compute_workspace_revision(tmp_path),
            requirement="review",
            acceptance_text="",
            patch=current_patch(tmp_path),
            verification_text="",
            contract_evidence=[],
        )
    )
    assert result.status == "unavailable"
    assert result.findings == []
    refreshed = store.get_run(run.run_id)
    assert refreshed is not None
    assert refreshed.review["status"] == "unavailable"


def test_completed_review_reused_by_call_id(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(INCOMPLETE_APP, encoding="utf-8")
    _git_init(tmp_path, "app.py")
    store = InMemoryTaskStore()
    session = store.create_session("ws")
    run = store.create_run(session.session_id, "review")
    revision = compute_workspace_revision(tmp_path)
    call_id = forced_review_call_id(run.run_id, revision)
    first = asyncio.run(
        LocalReviewService(
            tmp_path,
            store=store,
            model_complete=FakeAsyncModelClient(
                [ModelTurnResult(content='{"findings":[]}', finish_reason="stop")]
            ).complete,
            settings=_settings(tmp_path),
        ).run(
            run_id=run.run_id,
            subtask_id="r-reuse",
            workspace_revision=revision,
            requirement="review",
            acceptance_text="",
            patch="",
            verification_text="",
            contract_evidence=[],
        )
    )
    assert first.status == "completed"
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(
            name="delegate_review",
            arguments={"run_id": run.run_id, "subtask_id": "r-reuse", "workspace_revision": revision},
            call_id=call_id,
            provider_call_id=call_id,
        ),
        result=ToolResult(success=True, summary="done", stdout=first.model_dump_json(), call_id=call_id),
    )
    existing = store.get_tool_execution(call_id)
    assert existing is not None and existing.result is not None
    from review_agent.harness.coding_graph import CodingDeps, _execute_one_tool
    from review_agent.harness.tools import build_default_tool_registry

    settings = _settings(tmp_path)
    deps = CodingDeps(
        workspace_root=tmp_path,
        store=store,
        model_complete=FakeAsyncModelClient([]).complete,
        tool_registry=build_default_tool_registry(tmp_path, settings=settings),
        approval_policy=ApprovalPolicy("auto"),
        memory=AgentMemory(tmp_path, settings=settings),
        acceptance_gate=None,  # type: ignore[arg-type]
        context_assembler=None,  # type: ignore[arg-type]
        settings=settings,
    )

    async def _reuse() -> ToolResult:
        result, _approval = await _execute_one_tool(
            deps,
            ToolCall(name="delegate_review", arguments=dict(existing.tool_call.arguments), call_id=call_id, provider_call_id=call_id),
            run_id=run.run_id,
        )
        return result

    reused = asyncio.run(_reuse())
    assert reused.stdout == existing.result.stdout
    assert reused.call_id == call_id

    async def changed_focus() -> ToolResult:
        changed = existing.tool_call.model_copy(update={"arguments": {**existing.tool_call.arguments, "focus": "different"}})
        result, _ = await _execute_one_tool(deps, changed, run_id=run.run_id)
        return result

    refused = asyncio.run(changed_focus())
    assert not refused.success and refused.error_code == "invalid_arguments"

    # The latest projection may belong to another target; recovery uses the matching ledger row.
    from review_agent.harness.reviewer import prepare_review
    from review_agent.harness.coding_graph import _apply_review_gate
    resolved, _, _, cache_key = prepare_review(tmp_path, run=store.get_run(run.run_id),
        store=store, settings=settings, profile_id="deepseek")
    completed = first.model_copy(update={"target": resolved, "cache_key": cache_key})
    gate_call = forced_review_call_id(run.run_id, revision, cache_key)
    store.record_tool_execution(run_id=run.run_id, session_id=session.session_id,
        tool_call=ToolCall(name="delegate_review", arguments=dict(existing.tool_call.arguments), call_id=gate_call),
        result=ToolResult(success=True, summary="done", stdout=completed.model_dump_json(), call_id=gate_call))
    store.update_run(run.run_id, review={"mode": "default", "status": "completed", "cache_key": "other"})
    update = asyncio.run(_apply_review_gate(deps, {"run_id": run.run_id, "session_id": session.session_id,
        "changed_files": ["app.py"]}, store.get_run(run.run_id), revision))
    assert update is None
    assert store.get_run(run.run_id).review["cache_key"] == cache_key


def test_reviewer_off_is_recorded_and_skips_delegate(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(STUB_APP, encoding="utf-8")
    (tmp_path / "check_app.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    _git_init(tmp_path, "app.py", "check_app.py")
    class OffModel(FakeAsyncModelClient):
        async def complete(self, messages, tools=None, **kwargs):
            assert not {"delegate_review", "submit_review_response"} & {tool.name for tool in tools or []}
            return await super().complete(messages, tools, **kwargs)

    model = OffModel(
        [
            ModelTurnResult(
                tool_calls=[_tool("apply_patch", {"patch": _patch(STUB_APP, INCOMPLETE_APP)})],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(content="done without reviewer", finish_reason="stop"),
        ]
    )
    runtime = _runtime(tmp_path, model)
    session_id = runtime.create_session()
    run_id = runtime.start_run(
        session_id,
        "implement endpoint",
        acceptance=AcceptanceSpec(mode="commands", checks=[[sys.executable, "check_app.py"]]),
        reviewer="off",
    )
    model.turns.insert(1, ModelTurnResult(tool_calls=[_tool("delegate_review", {
        "run_id": run_id, "subtask_id": "must-not-start", "workspace_revision": "any"})]))
    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _r: True))
    run = runtime.task_store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.SUCCEEDED
    assert run.review is not None
    assert run.review["mode"] == "off"
    assert not any(event.type == "delegate.completed" for event in runtime.task_store.list_events(run_id))
    rejected = [item for item in runtime.task_store.list_tool_executions(run_id)
                if item.tool_call.name == "delegate_review"]
    assert len(rejected) == 1 and not rejected[0].result.success
    assert final.message == "done without reviewer"


def test_required_review_failure_marks_interrupted(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(STUB_APP, encoding="utf-8")
    (tmp_path / "check_app.py").write_text(CHECK_SCRIPT, encoding="utf-8")
    _git_init(tmp_path, "app.py", "check_app.py")
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(
                tool_calls=[_tool("apply_patch", {"patch": _patch(STUB_APP, INCOMPLETE_APP)})],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(content="ready", finish_reason="stop"),
            ModelTurnResult(content="not-json", finish_reason="stop"),
        ]
    )
    runtime = _runtime(tmp_path, model)
    session_id = runtime.create_session()
    run_id = runtime.start_run(
        session_id,
        "implement endpoint",
        acceptance=AcceptanceSpec(mode="commands", checks=[[sys.executable, "check_app.py"]]),
    )
    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _r: True))
    run = runtime.task_store.get_run(run_id)
    assert run is not None
    assert run.status == RunStatus.INTERRUPTED
    assert run.review is not None
    assert run.review["status"] != "completed"
    ws = runtime.get_run_workspace(run_id)
    assert ws is not None
    assert "create_item" in (ws.source_root / "app.py").read_text(encoding="utf-8")
    assert final.run_status == RunStatus.INTERRUPTED.value


def test_review_target_cache_focus_paths_evidence_and_baseline(tmp_path: Path) -> None:
    from review_agent.harness.tools import attach_review_tools, build_default_tool_registry

    (tmp_path / "app.py").write_text("value = 1\n")
    (tmp_path / "other.py").write_text("other = 1\n")
    _git_init(tmp_path, "app.py", "other.py")
    (tmp_path / "app.py").write_text("value = 2\n")
    store = InMemoryTaskStore()
    run = store.create_run(store.create_session("ws").session_id, "review")
    settings = _settings(tmp_path, review_agent_reviewer_max_steps=20)
    model = FakeAsyncModelClient([ModelTurnResult(content='{"findings":[]}') for _ in range(10)])
    registry = build_default_tool_registry(tmp_path, settings=settings)
    attach_review_tools(registry, workspace_root=tmp_path, store=store, model_complete=model.complete,
                        settings=settings, profile_id="deepseek", emit=lambda *_: None)

    def review(**extra):
        args = {"run_id": run.run_id, "subtask_id": "r1", "workspace_revision": compute_workspace_revision(tmp_path), **extra}
        validated = registry.validate_arguments("delegate_review", args)
        assert not isinstance(validated, ToolResult)
        tool, args = validated
        return asyncio.run(registry.execute_async(tool, args))

    first = review(focus="errors")
    assert first.success
    key = json.loads(first.stdout)["cache_key"]
    reused = review(focus="errors")
    assert "Reused" in reused.summary and json.loads(reused.stdout)["cache_key"] == key
    changed_focus = review(focus="performance")
    assert "Reused" not in changed_focus.summary
    assert json.loads(changed_focus.stdout)["cache_key"] != key
    scoped = review(target={"paths": ["app.py"]}, focus="performance")
    assert "Reused" not in scoped.summary
    store.update_run(run.run_id, verification={"status": "passed", "evidence_refs": ["new-evidence"]})
    assert "Reused" not in review(target={"paths": ["app.py"]}, focus="performance").summary
    store.record_tool_execution(run_id=run.run_id, session_id=run.session_id,
        tool_call=ToolCall(name="mcp__contracts__validate_response_sample", call_id="evidence"),
        result=ToolResult(success=True, summary="new contract evidence"))
    assert "Reused" not in review(target={"paths": ["app.py"]}, focus="performance").summary
    subprocess.run(["git", "add", "app.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.com", "commit", "-m", "new baseline"],
                   cwd=tmp_path, check=True, capture_output=True)
    assert "Reused" not in review(target={"paths": ["app.py"]}, focus="performance").summary
    assert not review(target={"baseline": "wrong"}).success


def test_paths_review_uses_real_content_and_reports_unchecked(tmp_path: Path) -> None:
    from review_agent.harness.review_target import ReviewTarget
    from review_agent.harness.reviewer import run_delegated_review

    (tmp_path / "app.py").write_text("BUG_MARKER = 7\n")
    (tmp_path / "large.py").write_text("x" * 130000)
    (tmp_path / "binary.bin").write_bytes(b"\0\xff")
    _git_init(tmp_path, "app.py", "large.py", "binary.bin")
    store = InMemoryTaskStore()
    run = store.create_run(store.create_session("ws").session_id, "review selected paths")
    model = FakeAsyncModelClient([ModelTurnResult(content='{"findings":[]}')])
    result = asyncio.run(run_delegated_review(tmp_path, store=store, model_complete=model.complete,
        settings=_settings(tmp_path), profile_id="deepseek", run=run, subtask_id="paths",
        workspace_revision=compute_workspace_revision(tmp_path),
        target=ReviewTarget(kind="paths", paths=["./app.py", "large.py", "binary.bin", "missing.py"])))
    assert result.status == "completed"
    assert result.coverage == ["app.py"]
    assert result.target.paths == ["app.py", "binary.bin", "large.py", "missing.py"]
    assert "BUG_MARKER" in model.seen_messages[0][1].content
    assert "(empty git diff)" in model.seen_messages[0][1].content
    assert any("large.py" in x for x in result.unchecked)
    assert any("binary.bin" in x for x in result.unchecked)
    assert any("missing.py" in x for x in result.unchecked)
    assert not result.is_passed()
    assert result.read_records[0]["path"] == "app.py"


def test_patch_includes_untracked_and_reports_omissions(tmp_path: Path) -> None:
    from review_agent.harness.review_target import collect_patch

    (tmp_path / "app.py").write_text("v = 1\n")
    _git_init(tmp_path, "app.py")
    (tmp_path / "new 文件.py").write_text("NEW = True\n")
    (tmp_path / "empty.txt").write_text("")
    (tmp_path / "binary.bin").write_bytes(b"\0\xff")
    (tmp_path / "huge.txt").write_text("x" * 130000)
    (tmp_path / "diff_limit.txt").write_text("x\n" * 30000)
    before_index = (tmp_path / ".git/index").read_bytes()
    material = collect_patch(tmp_path)
    assert "+NEW = True" in material["patch"]
    assert "empty.txt" in material["paths"]
    assert any("binary.bin" in x for x in material["unchecked"])
    assert any("huge.txt" in x for x in material["unchecked"])
    assert any("diff_limit.txt" in x for x in material["unchecked"])
    assert (tmp_path / ".git/index").read_bytes() == before_index


def test_reviewer_usage_accumulates_and_unknown_stays_unknown(tmp_path: Path) -> None:
    from review_agent.harness.models import Usage
    import pytest

    (tmp_path / "app.py").write_text("v = 1\n")
    _git_init(tmp_path, "app.py")
    for unknown in (False, True):
        store = InMemoryTaskStore()
        run = store.create_run(store.create_session("ws").session_id, "review")
        model = FakeAsyncModelClient([
            ModelTurnResult(tool_calls=[_tool("read_file", {"path": "app.py"})],
                usage=None if unknown else Usage(profile_id="deepseek", input_tokens=10, output_tokens=3, estimated_cost=0.1)),
            ModelTurnResult(content='{"findings":[]}', usage=Usage(profile_id="deepseek", input_tokens=20, output_tokens=4, estimated_cost=0.2)),
        ])
        result = asyncio.run(LocalReviewService(tmp_path, store=store, model_complete=model.complete,
            settings=_settings(tmp_path)).run(run_id=run.run_id, subtask_id="usage",
                workspace_revision=compute_workspace_revision(tmp_path), requirement="review", acceptance_text="",
                patch="", verification_text="", contract_evidence=[]))
        assert result.usage.input_tokens == (None if unknown else 30)
        assert result.usage.output_tokens == (None if unknown else 7)
        assert result.usage.estimated_cost is None if unknown else result.usage.estimated_cost == pytest.approx(0.3)
        assert store.get_run(run.run_id).usage["input_tokens"] == result.usage.input_tokens
        assert store.get_run(run.run_id).usage["estimated_cost"] == result.usage.estimated_cost


def test_review_rejects_invalid_entries_and_detects_mid_review_mutation(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("v = 1\n")
    _git_init(tmp_path, "app.py")
    for content in ('{"findings":[null]}', '{"findings":[{}]}', '{"findings":[]}'):
        store = InMemoryTaskStore()
        run = store.create_run(store.create_session("ws").session_id, "review")

        async def model(*args, **kwargs):
            if content == '{"findings":[]}':
                (tmp_path / "app.py").write_text("v = 2\n")
            return ModelTurnResult(content=content)

        result = asyncio.run(LocalReviewService(tmp_path, store=store, model_complete=model,
            settings=_settings(tmp_path)).run(run_id=run.run_id, subtask_id="invalid",
                workspace_revision=compute_workspace_revision(tmp_path), requirement="review", acceptance_text="",
                patch="", verification_text="", contract_evidence=[]))
        assert result.status in {"failed", "unavailable"}
        assert not result.is_passed()


def test_original_review_tools_read_matching_snapshot(tmp_path: Path) -> None:
    from review_agent.harness.review_target import ReviewTarget
    from review_agent.harness.reviewer import run_delegated_review, review_is_current
    from review_agent.services.workspace_manager import WorkspaceManager

    (tmp_path / "unrelated.txt").write_text("ancestor repository\n")
    _git_init(tmp_path, "unrelated.txt")
    (tmp_path / "unrelated.txt").write_text("unrelated change\n")
    origin = tmp_path / "repo"
    origin.mkdir()
    (origin / "app.py").write_text("v = 1\n")
    _git_init(origin, "app.py")
    (origin / "app.py").write_text("v = 2\n")
    manager = WorkspaceManager(tmp_path / "data")
    manager.register("ws", origin)
    workspace = manager.prepare_run("ws", "run")
    (workspace.source_root / "app.py").write_text("v = 3\n")
    store = InMemoryTaskStore()
    run = store.create_run(store.create_session("ws").session_id, "review original changes")
    model = FakeAsyncModelClient([
        ModelTurnResult(tool_calls=[_tool("read_file", {"path": "app.py"}), _tool("git_diff", {"path": "app.py"}), _tool("git_status", {})]),
        ModelTurnResult(content='{"findings":[]}'),
    ])
    result = asyncio.run(run_delegated_review(workspace.source_root, store=store, model_complete=model.complete,
        settings=_settings(tmp_path), profile_id="deepseek", run=run, subtask_id="original",
        workspace_revision=compute_workspace_revision(workspace.source_root),
        target=ReviewTarget(kind="workspace_changes")))
    assert result.status == "completed"
    observations = [m.content for m in model.seen_messages[-1] if m.role == "tool"]
    assert any("stdout=v = 2" in m for m in observations)
    assert any("+v = 2" in m and "-v = 1" in m for m in observations)
    assert all("v = 3" not in m and "unrelated.txt" not in m for m in observations)
    assert any("snapshot, not a live Git status" in m and "app.py" in m for m in observations)
    assert result.selected_paths == ["app.py"]
    assert not review_is_current(result.as_review_dict(), result.workspace_revision)
    assert (origin / "app.py").read_text() == "v = 2\n"


def test_reviewer_timeout_keeps_unknown_usage(tmp_path: Path) -> None:
    from review_agent.harness.models import Usage

    (tmp_path / "app.py").write_text("v = 1\n")
    _git_init(tmp_path, "app.py")
    store = InMemoryTaskStore()
    run = store.create_run(store.create_session("ws").session_id, "review")
    turns = iter([ModelTurnResult(tool_calls=[_tool("read_file", {"path": "app.py"})],
                  usage=Usage(profile_id="deepseek", input_tokens=10, output_tokens=2))])

    async def model(*args, **kwargs):
        turn = next(turns, None)
        if turn is None:
            raise TimeoutError()
        return turn

    result = asyncio.run(LocalReviewService(tmp_path, store=store, model_complete=model,
        settings=_settings(tmp_path)).run(run_id=run.run_id, subtask_id="timeout",
            workspace_revision=compute_workspace_revision(tmp_path), requirement="review", acceptance_text="",
            patch="", verification_text="", contract_evidence=[]))
    assert result.status == "interrupted"
    assert result.usage.input_tokens is None
    assert store.get_run(run.run_id).budget["reviewer_step_count"] == 2
    assert store.get_run(run.run_id).usage["input_tokens"] is None


def test_delivery_review_waits_for_current_acceptance_before_spending_budget(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("value = 1\n")
    _git_init(tmp_path, "app.py")

    class EarlyReviewModel:
        parent_turn = 0

        async def complete(self, messages, tools=None, **kwargs):
            if any("read-only code reviewer" in m.content for m in messages if m.role == "system"):
                return ModelTurnResult(content='{"findings":[]}')
            self.parent_turn += 1
            if self.parent_turn == 1:
                return ModelTurnResult(tool_calls=[_tool("apply_patch", {"patch": _patch("value = 1\n", "value = 2\n")})])
            if self.parent_turn == 2:
                workspace = runtime.get_run_workspace(rid)
                return ModelTurnResult(tool_calls=[_tool("delegate_review", {"run_id": rid,
                    "subtask_id": "early", "workspace_revision": compute_workspace_revision(workspace.source_root)})])
            return ModelTurnResult(content="done")

    runtime = _runtime(tmp_path, EarlyReviewModel(), settings=_settings(tmp_path, review_agent_reviewer_max_steps=1))
    rid = runtime.start_run(runtime.create_session(), "set value to 2", acceptance=AcceptanceSpec(
        mode="commands", checks=[[sys.executable, "-c", "from app import value; assert value == 2"]]))
    final = asyncio.run(runtime.execute_run(rid))
    run = runtime.task_store.get_run(rid)
    assert run.status == RunStatus.SUCCEEDED, final.message
    assert run.verification["status"] == "passed"
    assert run.review["status"] == "completed"
    assert run.review["workspace_revision"] == run.verification["workspace_revision"]
    events = runtime.task_store.list_events(rid)
    assert next(e.seq for e in events if e.type == "verification.completed") < next(
        e.seq for e in events if e.type == "delegate.started")


def test_reviewer_can_correct_format_once_within_existing_budget(tmp_path: Path) -> None:
    from review_agent.harness.models import Usage

    (tmp_path / "app.py").write_text("value = 1\n")
    _git_init(tmp_path, "app.py")
    for steps, expected in [(2, "completed"), (1, "unavailable")]:
        store = InMemoryTaskStore()
        run = store.create_run(store.create_session("ws").session_id, "review")
        model = FakeAsyncModelClient([
            ModelTurnResult(content="No bugs found. Checked {value: 1}.",
                usage=Usage(profile_id="deepseek", input_tokens=10, output_tokens=5)),
            ModelTurnResult(content='{"findings":[]}',
                usage=Usage(profile_id="deepseek", input_tokens=12, output_tokens=4)),
        ])
        result = asyncio.run(LocalReviewService(tmp_path, store=store, model_complete=model.complete,
            settings=_settings(tmp_path, review_agent_reviewer_max_steps=steps)).run(
                run_id=run.run_id, subtask_id="format", workspace_revision=compute_workspace_revision(tmp_path),
                requirement="review", acceptance_text="", patch="", verification_text="", contract_evidence=[]))
        assert result.status == expected
        assert result.usage.input_tokens == (22 if steps == 2 else 10)
        assert store.get_run(run.run_id).budget["reviewer_step_count"] == steps
