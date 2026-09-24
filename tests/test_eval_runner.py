from __future__ import annotations

import json
import uuid
from pathlib import Path

from review_agent.config import Settings
from review_agent.eval.acceptance import run_hidden_acceptance
from review_agent.eval.classify import classify_outcome
from review_agent.eval.models import EvalTask, HiddenOutcome
from review_agent.eval.runner import apply_variant, run_eval_task
from review_agent.eval.snapshot import materialize_sample
from review_agent.eval.summarize import summarize_jsonl
from review_agent.harness.models import ModelTurnResult, ToolCall
from review_agent.harness.runtime import AgentRuntime
from review_agent.harness.tools import build_default_tool_registry
from tests.test_coding_runtime import FakeAsyncModelClient


def _settings(tmp_path: Path, **kwargs) -> Settings:
    data = {
        "review_agent_data_root": str(tmp_path / "data"),
        "review_agent_approval_mode": "auto",
        "review_agent_executor_backend": "host",
        "review_agent_agent_max_steps": 8,
        "review_agent_skills_enabled": True,
        "review_agent_context_optimization": True,
    }
    data.update(kwargs)
    return Settings(**data)


def _tiny_sample(tmp_path: Path) -> Path:
    sample = tmp_path / "sample"
    sample.mkdir()
    (sample / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return sample


def _hidden_ok(tmp_path: Path) -> Path:
    hidden = tmp_path / "hidden" / "ok"
    hidden.mkdir(parents=True)
    (hidden / "check.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "text = Path(os.environ['EVAL_WORKSPACE']).joinpath('app.py').read_text(encoding='utf-8')\n"
        "assert 'VALUE = 2' in text, text\n",
        encoding="utf-8",
    )
    return hidden / "check.py"


def _task(tmp_path: Path, script: Path, task_id: str = "demo-fix") -> EvalTask:
    return EvalTask(
        task_id=task_id,
        set="dev",
        category="fix",
        sample="tiny",
        tree_hash="",
        requirement="Set VALUE to 2.",
        allowed_paths=["app.py"],
        budget={"max_steps": 8, "max_verification_repairs": 2},
        hidden_script=script,
    )


def _patch_turn(patch: str) -> ModelTurnResult:
    call_id = str(uuid.uuid4())
    return ModelTurnResult(
        tool_calls=[
            ToolCall(name="apply_patch", arguments={"patch": patch}, call_id=call_id, provider_call_id=call_id)
        ],
        finish_reason="tool_calls",
    )


GOOD_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""

BAD_PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 9
"""


def test_eval_success_uses_hidden_acceptance(tmp_path: Path) -> None:
    sample = _tiny_sample(tmp_path)
    script = _hidden_ok(tmp_path)
    model = FakeAsyncModelClient(
        [
            _patch_turn(GOOD_PATCH),
            ModelTurnResult(content="done", finish_reason="stop"),
        ]
    )
    result = run_eval_task(
        _task(tmp_path, script),
        variant="baseline",
        repo_root=tmp_path,
        model_client=model,
        settings=_settings(tmp_path),
        sample_root=sample,
        code_commit="deadbeef",
    )
    assert result.outcome == "success"
    assert result.hidden_passed is True
    assert result.hidden_exit_code == 0
    assert result.variant == "baseline"
    assert result.reviewer == "off"
    assert result.skills_enabled is False
    assert result.context_optimization is False
    assert result.code_commit == "deadbeef"
    assert result.tree_hash
    assert result.profile_id == "deepseek"
    assert result.budget is not None
    assert result.budget.get("max_steps") == 8


def test_eval_implementation_failure(tmp_path: Path) -> None:
    sample = _tiny_sample(tmp_path)
    script = _hidden_ok(tmp_path)
    model = FakeAsyncModelClient(
        [
            _patch_turn(BAD_PATCH),
            ModelTurnResult(content="done", finish_reason="stop"),
        ]
    )
    result = run_eval_task(
        _task(tmp_path, script, task_id="demo-impl"),
        variant="full",
        repo_root=tmp_path,
        model_client=model,
        settings=_settings(tmp_path),
        sample_root=sample,
    )
    assert result.hidden_passed is False
    assert result.outcome in {"implementation", "verification"}
    assert result.reviewer == "default"
    assert result.skills_enabled is True


def test_eval_environment_failure(tmp_path: Path) -> None:
    sample = _tiny_sample(tmp_path)
    script = _hidden_ok(tmp_path)
    model = FakeAsyncModelClient([ModelTurnResult(content="done", finish_reason="stop")])
    result = run_eval_task(
        _task(tmp_path, script, task_id="demo-env"),
        variant="baseline",
        repo_root=tmp_path,
        model_client=model,
        settings=_settings(tmp_path),
        sample_root=sample,
        python_executable="/no/such/eval-python",
    )
    assert result.outcome == "environment"
    assert result.hidden_passed is False


def test_hidden_script_rejected_inside_workspace(tmp_path: Path) -> None:
    sample = _tiny_sample(tmp_path)
    dest = tmp_path / "ws"
    materialize_sample(sample, dest)
    inside = dest / "check.py"
    inside.write_text("assert False\n", encoding="utf-8")
    outcome = run_hidden_acceptance(inside, dest)
    assert outcome.could_not_run is True
    assert "must not live inside" in outcome.stderr


def test_baseline_and_full_share_snapshot_and_budget(tmp_path: Path) -> None:
    sample = _tiny_sample(tmp_path)
    script = _hidden_ok(tmp_path)
    settings = _settings(tmp_path)
    rows = []
    for variant in ("baseline", "full"):
        model = FakeAsyncModelClient([ModelTurnResult(content="done", finish_reason="stop")])
        rows.append(
            run_eval_task(
                _task(tmp_path, script, task_id=f"demo-{variant}"),
                variant=variant,
                repo_root=tmp_path,
                model_client=model,
                settings=settings,
                sample_root=sample,
                code_commit="abc123",
            )
        )
    assert rows[0].tree_hash == rows[1].tree_hash
    assert rows[0].budget["max_steps"] == rows[1].budget["max_steps"]
    assert rows[0].profile_id == rows[1].profile_id
    assert rows[0].code_commit == rows[1].code_commit == "abc123"
    assert rows[0].skills_enabled is False
    assert rows[1].skills_enabled is True


def test_skills_flag_drops_skill_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    off = _settings(tmp_path, review_agent_skills_enabled=False)
    runtime = AgentRuntime(
        workspace,
        model_client=FakeAsyncModelClient([]),
        settings=off,
        isolate_workspace=False,
    )
    names = {spec.name for spec in runtime.tool_registry.specs()}
    assert "load_skill" not in names
    from review_agent.harness.skills import SkillLoader

    on = build_default_tool_registry(
        workspace,
        settings=_settings(tmp_path, review_agent_skills_enabled=True),
        skill_loader=SkillLoader(workspace),
    )
    assert "load_skill" in {spec.name for spec in on.specs()}


def test_summarize_jsonl_known_rows(tmp_path: Path) -> None:
    path = tmp_path / "rows.jsonl"
    path.write_text(
        json.dumps({"outcome": "success", "variant": "baseline"})
        + "\n"
        + json.dumps({"outcome": "environment", "variant": "baseline"})
        + "\n"
        + json.dumps({"outcome": "implementation", "variant": "full"})
        + "\n",
        encoding="utf-8",
    )
    summary = summarize_jsonl(path)
    assert summary["attempts"] == 3
    assert summary["successes"] == 1
    assert summary["completion_rate"] == 1 / 3
    assert summary["outcomes"]["environment"] == 1
    assert summary["by_variant"]["baseline"]["success"] == 1


def test_apply_variant_and_classify_environment() -> None:
    settings = Settings(review_agent_skills_enabled=True, review_agent_context_optimization=True)
    baseline = apply_variant(settings, "baseline")
    assert baseline.review_agent_skills_enabled is False
    assert baseline.review_agent_context_optimization is False
    hidden = HiddenOutcome(passed=False, exit_code=None, could_not_run=True)
    assert classify_outcome(hidden=hidden, run_status="succeeded", budget=None) == "environment"
    stream_failed = HiddenOutcome(passed=False, exit_code=1, could_not_run=False)
    events = [
        type(
            "E",
            (),
            {
                "type": "model.error",
                "message": "Model error: Model stream failed: Connection error.",
                "payload": {"error_code": "stream_failed"},
            },
        )()
    ]
    assert (
        classify_outcome(
            hidden=stream_failed,
            run_status="failed",
            budget={"step_count": 1, "max_steps": 24},
            events=events,
        )
        == "environment"
    )


def test_eval_model_connection_failure_is_environment_not_implementation(tmp_path: Path) -> None:
    sample = _tiny_sample(tmp_path)
    script = _hidden_ok(tmp_path)
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(
                finish_reason="error",
                raw_error="Model stream failed: Connection error. | All connection attempts failed",
                error_code="stream_failed",
            )
        ]
    )
    result = run_eval_task(
        _task(tmp_path, script, task_id="demo-conn"),
        variant="baseline",
        repo_root=tmp_path,
        model_client=model,
        settings=_settings(tmp_path),
        sample_root=sample,
    )
    assert result.outcome == "environment"
    assert result.hidden_passed is False
    assert result.run_status == "failed"
    assert result.failure_reason is not None
    assert "Connection error" in result.failure_reason
    assert result.failure_reason != "implementation"


def test_eval_proxy_warning_when_local_proxy_down(monkeypatch) -> None:
    from review_agent.cli import _unreachable_local_proxy_warning

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("ALL_PROXY", raising=False)
    warning = _unreachable_local_proxy_warning()
    assert warning is not None
    assert "not reachable" in warning


def test_eval_failure_reason_uses_model_error_event() -> None:
    from types import SimpleNamespace

    from review_agent.eval.runner import _failure_reason

    events = [
        SimpleNamespace(type="run.created", message="created"),
        SimpleNamespace(
            type="model.error",
            message="Model error: Model stream failed: Connection error. | Connection refused",
        ),
    ]
    reason = _failure_reason(events, platform_error=None, outcome="environment")
    assert reason is not None
    assert "Connection error" in reason
    assert reason != "implementation"

