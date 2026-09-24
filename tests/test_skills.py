from __future__ import annotations

import os
from pathlib import Path

import pytest

from review_agent.config import Settings
from review_agent.harness.approval import ApprovalDecision, ApprovalPolicy
from review_agent.harness.models import ToolResult
from review_agent.harness.skills import BUILTIN_ROOT, SkillLoader
from review_agent.harness.tools import ExecutionContext, build_default_tool_registry
from review_agent.services.executor import Executor


REQUIRED_SKILLS = {
    "fix-failing-tests",
    "implement-fastapi-endpoint",
    "integrate-llm-provider",
}


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    return root


def test_discover_builtin_skills_without_body_in_hints(workspace: Path) -> None:
    loader = SkillLoader(workspace)
    discovered = loader.discover()
    assert REQUIRED_SKILLS <= set(discovered)
    for name in REQUIRED_SKILLS:
        assert discovered[name].source == "builtin"
        assert discovered[name].root.is_relative_to(BUILTIN_ROOT)

    hints = loader.list_hints()
    hint_blob = "\n".join(f"{h['name']}:{h['description']}" for h in hints)
    assert "Minimal fix" not in hint_blob
    assert "Required evidence" not in hint_blob
    assert all("description" in item and "name" in item for item in hints)

    loaded = loader.load("fix-failing-tests")
    assert "Minimal fix" in loaded.body
    assert loaded.body_hash
    assert loaded.record.source == "builtin"


def test_project_skill_overrides_builtin(workspace: Path) -> None:
    skill_dir = workspace / ".agents" / "skills" / "fix-failing-tests"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: fix-failing-tests\n"
        "description: Project override for failing tests.\n"
        "---\n\n"
        "# Project body marker UNIQUE_PROJECT_BODY\n",
        encoding="utf-8",
    )
    loader = SkillLoader(workspace)
    record = loader.get("fix-failing-tests")
    assert record is not None
    assert record.source == "project"
    loaded = loader.load("fix-failing-tests")
    assert "UNIQUE_PROJECT_BODY" in loaded.body
    assert loaded.record.source == "project"


def test_load_skill_tool_emits_pending_event_and_resource_boundaries(workspace: Path) -> None:
    loader = SkillLoader(workspace)
    ctx = ExecutionContext(run_id="run-1", call_id="c-load")
    settings = Settings(review_agent_approval_mode="auto", review_agent_executor_backend="host")
    registry = build_default_tool_registry(
        workspace,
        settings=settings,
        execution_context=ctx,
        skill_loader=loader,
    )

    outcome = registry.validate_arguments(
        "load_skill", {"name": "fix-failing-tests"}, call_id="c-load"
    )
    assert not isinstance(outcome, ToolResult)
    tool, args = outcome
    result = registry.execute(tool, args, call_id="c-load")
    assert result.success
    assert "Reproduce" in (result.stdout or "")
    assert ctx.pending_skill_events
    event = ctx.pending_skill_events[0]
    assert event["name"] == "fix-failing-tests"
    assert event["body_hash"]
    assert event["source"] == "builtin"
    assert event["source_path"]

    ctx.pending_skill_events.clear()
    outcome = registry.validate_arguments(
        "read_skill_resource",
        {"name": "fix-failing-tests", "path": "references/evidence-checklist.md"},
        call_id="c-ref",
    )
    assert not isinstance(outcome, ToolResult)
    tool, args = outcome
    ref = registry.execute(tool, args, call_id="c-ref")
    assert ref.success
    assert "Reproduce command" in (ref.stdout or "")
    assert ctx.last_execution_meta["skill_resource"]["content_hash"]

    bad = registry.validate_arguments(
        "read_skill_resource",
        {"name": "fix-failing-tests", "path": "../implement-fastapi-endpoint/SKILL.md"},
        call_id="c-bad",
    )
    assert not isinstance(bad, ToolResult)
    tool, args = bad
    denied = registry.execute(tool, args, call_id="c-bad")
    assert not denied.success
    assert denied.error_code == "execution_error"

    # Escaping symlink under a project skill root (do not mutate package builtins).
    proj = workspace / ".agents" / "skills" / "fix-failing-tests"
    (proj / "references").mkdir(parents=True)
    (proj / "SKILL.md").write_text(
        "---\nname: fix-failing-tests\ndescription: project for symlink test\n---\n\nbody\n",
        encoding="utf-8",
    )
    (proj / "references" / "ok.md").write_text("ok\n", encoding="utf-8")
    outside = workspace / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    link = proj / "references" / "escape-link.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink not available")
    loader = SkillLoader(workspace)
    registry = build_default_tool_registry(
        workspace,
        settings=settings,
        execution_context=ctx,
        skill_loader=loader,
    )
    outcome = registry.validate_arguments(
        "read_skill_resource",
        {"name": "fix-failing-tests", "path": "references/escape-link.md"},
        call_id="c-link",
    )
    assert not isinstance(outcome, ToolResult)
    tool, args = outcome
    link_result = registry.execute(tool, args, call_id="c-link")
    assert not link_result.success
    summary = (link_result.summary or "").lower()
    assert "symlink" in summary or "skill root" in summary


def test_run_skill_script_uses_executor(workspace: Path, tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    executor = Executor(backend="host", artifact_root=artifacts)
    ctx = ExecutionContext(run_id="run-script", call_id="c-script")
    settings = Settings(review_agent_approval_mode="auto", review_agent_executor_backend="host")

    script_skill = workspace / ".agents" / "skills" / "fix-failing-tests"
    script_skill.mkdir(parents=True)
    (script_skill / "SKILL.md").write_text(
        "---\nname: fix-failing-tests\ndescription: override with script\n---\n\nbody\n",
        encoding="utf-8",
    )
    scripts = script_skill / "scripts"
    scripts.mkdir()
    script = scripts / "hello.sh"
    script.write_text("#!/usr/bin/env bash\necho skill-script-ok\n", encoding="utf-8")
    script.chmod(0o755)

    loader = SkillLoader(workspace)
    registry = build_default_tool_registry(
        workspace,
        settings=settings,
        executor=executor,
        execution_context=ctx,
        skill_loader=loader,
    )

    policy = ApprovalPolicy("confirm")
    spec = registry.get("run_skill_script").spec
    decision = policy.evaluate(
        spec, {"name": "fix-failing-tests", "path": "scripts/hello.sh"}
    )
    assert decision.decision == ApprovalDecision.CONFIRM

    outcome = registry.validate_arguments(
        "run_skill_script",
        {"name": "fix-failing-tests", "path": "scripts/hello.sh"},
        call_id="c-script",
    )
    assert not isinstance(outcome, ToolResult)
    tool, args = outcome
    result = registry.execute(tool, args, call_id="c-script")
    assert result.success
    assert "skill-script-ok" in (result.stdout or "")
    assert ctx.last_execution_meta.get("execution_id")
    assert ctx.last_execution_meta.get("argv")
    assert result.artifact_refs or ctx.last_execution_meta.get("stdout_artifact")


def test_implement_fastapi_skill_walkthrough(workspace: Path) -> None:
    """One full skill path: discover → load → read contract reference → evidence keys present."""
    loader = SkillLoader(workspace)
    assert "implement-fastapi-endpoint" in loader.discover()
    hints = {h["name"]: h["description"] for h in loader.list_hints()}
    assert "FastAPI" in hints["implement-fastapi-endpoint"]
    assert "POST /items" not in hints["implement-fastapi-endpoint"]

    loaded = loader.load("implement-fastapi-endpoint")
    assert "Contract source" in loaded.body or "contract source" in loaded.body.lower()
    assert "mcp__api_contract__get_endpoint_contract" in loaded.body
    text, content_hash = loader.read_resource(
        "implement-fastapi-endpoint",
        "references/contract-example.md",
    )
    assert "POST /items" in text
    assert content_hash
    lower = loaded.body.lower()
    assert "contract source" in lower
    assert "executable interface check" in lower or "interface check" in lower
    assert "change" in lower

    for name in ("fix-failing-tests", "integrate-llm-provider"):
        meta = loader.get(name)
        assert meta is not None
        assert meta.description
        body = loader.load(name).body
        assert body.strip()
