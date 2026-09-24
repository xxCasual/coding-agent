"""Decision helpers for scripts/compose-demo-run.py (no HTTP)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_demo_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "compose-demo-run.py"
    spec = importlib.util.spec_from_file_location("compose_demo_run", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def demo_script():
    return _load_demo_script()


def test_api_base_ignores_non_url_argv(demo_script) -> None:
    assert demo_script.api_base(["pytest", "tests/test_compose_demo_run.py"]) == demo_script.DEFAULT_API
    assert demo_script.api_base(["compose-demo-run.py", "http://127.0.0.1:9000"]) == "http://127.0.0.1:9000"


def test_should_not_auto_resume_patch_reconciliation(demo_script) -> None:
    run = {
        "status": "needs_attention",
        "phase": "run.reconciliation",
        "attention": {
            "reason": "Patch is partially applied or not checkable; files must be reconciled.",
            "call_id": "p1",
        },
    }
    assert demo_script.is_patch_reconciliation(run) is True
    assert demo_script.should_auto_resume(run, resumed_call_ids=set()) is False


def test_should_not_auto_resume_reason_without_phase(demo_script) -> None:
    run = {
        "status": "needs_attention",
        "phase": "tool.apply_patch",
        "attention": {"reason": "Patch is partially applied or not checkable", "call_id": "p1"},
    }
    assert demo_script.is_patch_reconciliation(run) is True
    assert demo_script.should_auto_resume(run, resumed_call_ids=set()) is False


def test_should_auto_resume_other_attention_once(demo_script) -> None:
    run = {
        "status": "needs_attention",
        "phase": "mcp.disconnected",
        "attention": {"reason": "MCP server disconnected", "call_id": "m1"},
    }
    assert demo_script.should_auto_resume(run, resumed_call_ids=set()) is True
    assert demo_script.should_auto_resume(run, resumed_call_ids={"m1"}) is False


def test_should_auto_resume_orphan_attention_once(demo_script) -> None:
    run = {
        "status": "needs_attention",
        "phase": "run.reconciliation",
        "attention": {
            "reason": "Could not confirm that the previous command or container has exited.",
            "call_id": None,
            "unknown": ["c1"],
        },
    }
    assert demo_script.is_patch_reconciliation(run) is False
    assert demo_script.should_auto_resume(run, resumed_call_ids=set()) is True
    assert demo_script.should_auto_resume(run, resumed_call_ids={"_unknown"}) is False


def test_succeeded_is_terminal(demo_script) -> None:
    assert "succeeded" in demo_script.TERMINAL
    assert "completed" in demo_script.TERMINAL
