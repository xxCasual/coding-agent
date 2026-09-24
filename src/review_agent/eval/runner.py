from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from review_agent.config import Settings, get_settings
from review_agent.eval.acceptance import run_hidden_acceptance
from review_agent.eval.classify import classify_outcome
from review_agent.eval.models import EvalResult, EvalTask, EvalVariant
from review_agent.eval.snapshot import load_manifest, materialize_sample
from review_agent.harness.models import RunStatus
from review_agent.harness.task_store import InMemoryTaskStore
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager

REPO_ROOT = Path(__file__).resolve().parents[3]


def apply_variant(settings: Settings, variant: EvalVariant) -> Settings:
    if variant == "baseline":
        return settings.model_copy(
            update={
                "review_agent_skills_enabled": False,
                "review_agent_context_optimization": False,
            }
        )
    return settings.model_copy(
        update={
            "review_agent_skills_enabled": True,
            "review_agent_context_optimization": True,
        }
    )


def reviewer_for(variant: EvalVariant) -> str:
    return "off" if variant == "baseline" else "default"


def run_eval(
    manifest: Path,
    *,
    variant: EvalVariant = "baseline",
    out_dir: Path,
    repo_root: Path | None = None,
    task_ids: Iterable[str] | None = None,
    code_commit: str | None = None,
    model_client: Any | None = None,
    model_client_factory: Callable[[], Any] | None = None,
    settings: Settings | None = None,
    profile_id: str = "deepseek",
) -> list[EvalResult]:
    repo = (repo_root or REPO_ROOT).resolve()
    wanted = set(task_ids or [])
    tasks = [item for item in load_manifest(manifest, repo_root=repo) if not wanted or item.task_id in wanted]
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f"{variant}.jsonl"
    results: list[EvalResult] = []
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for task in tasks:
            result = run_eval_task(
                task,
                variant=variant,
                repo_root=repo,
                code_commit=code_commit,
                model_client=model_client,
                model_client_factory=model_client_factory,
                settings=settings,
                profile_id=profile_id,
            )
            results.append(result)
            handle.write(json.dumps(result.to_json(), ensure_ascii=False) + "\n")
    (out_dir / f"{variant}.summary.json").write_text(
        json.dumps({"variant": variant, "results": [item.to_json() for item in results]}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    return results


def run_eval_task(
    task: EvalTask,
    *,
    variant: EvalVariant,
    repo_root: Path,
    code_commit: str | None = None,
    model_client: Any | None = None,
    model_client_factory: Callable[[], Any] | None = None,
    settings: Settings | None = None,
    profile_id: str = "deepseek",
    sample_root: Path | None = None,
    python_executable: str | None = None,
) -> EvalResult:
    started = time.perf_counter()
    queued_ms: float | None = 0.0
    base_settings = settings or get_settings()
    variant_settings = apply_variant(base_settings, variant)
    max_steps = int(task.budget.get("max_steps") or 24)
    variant_settings = variant_settings.model_copy(update={"review_agent_agent_max_steps": max_steps})
    data_root = Path(variant_settings.review_agent_data_root).expanduser().resolve()
    origin = sample_root or (repo_root / "eval" / "samples" / task.sample)
    work = data_root / "eval-origins" / f"{task.task_id}-{uuid.uuid4().hex[:8]}"
    tree_hash = materialize_sample(origin, work)
    if task.tree_hash and task.tree_hash != tree_hash:
        hidden = run_hidden_acceptance(task.hidden_script, work, python_executable=python_executable)
        return _result(
            task,
            variant=variant,
            tree_hash=tree_hash,
            code_commit=code_commit,
            profile_id=profile_id,
            run_id=None,
            run_status=None,
            hidden=hidden,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            queued_ms=queued_ms,
            usage=None,
            budget=task.budget,
            workspace_revision=None,
            platform_error=f"tree_hash mismatch: expected {task.tree_hash} got {tree_hash}",
            skills_enabled=variant_settings.review_agent_skills_enabled,
            context_optimization=variant_settings.review_agent_context_optimization,
        )

    manager = WorkspaceManager(data_root)
    workspace_id = f"eval-{task.task_id}"
    manager.register(workspace_id, work, display_name=task.task_id)
    store = InMemoryTaskStore()
    service = TaskService(
        store,
        manager,
        variant_settings,
        model_client=model_client,
        model_client_factory=model_client_factory,
    )
    session = service.create_session(workspace_id)
    reviewer = reviewer_for(variant)
    queue_start = time.perf_counter()
    run = service.create_run(
        session.session_id,
        task.requirement,
        idempotency_key=str(uuid.uuid4()),
        profile_id=profile_id,
        reviewer=reviewer,
    )
    queued_ms = (time.perf_counter() - queue_start) * 1000
    platform_error = None
    run_status = run.status.value if hasattr(run.status, "value") else str(run.status)
    usage = run.usage
    budget = run.budget or dict(task.budget)
    workspace_revision = run.workspace_revision
    events: list[Any] = []
    try:
        final = service.run_once(run.run_id, approver=lambda _req: True)
        latest = store.get_run(run.run_id) or run
        run_status = final.run_status or (
            latest.status.value if hasattr(latest.status, "value") else str(latest.status)
        )
        usage = latest.usage
        budget = latest.budget or budget
        workspace_revision = latest.workspace_revision
        events = store.list_events(run.run_id)
    except Exception as exc:  # noqa: BLE001 — eval runner records platform failures
        platform_error = f"{type(exc).__name__}: {exc}"
        run_status = RunStatus.FAILED.value

    prepared = manager.get_run(run.run_id)
    check_root = prepared.source_root if prepared is not None else work
    hidden = run_hidden_acceptance(
        task.hidden_script,
        check_root,
        python_executable=python_executable,
    )
    tool_latency = _tool_latency_ms(events)
    usage_dict = dict(usage) if isinstance(usage, dict) else None
    model_latency = None if usage_dict is None else usage_dict.get("model_latency_ms")
    estimated = None if usage_dict is None else usage_dict.get("estimated_cost")
    return _result(
        task,
        variant=variant,
        tree_hash=tree_hash,
        code_commit=code_commit,
        profile_id=profile_id,
        run_id=run.run_id,
        run_status=run_status,
        hidden=hidden,
        elapsed_ms=(time.perf_counter() - started) * 1000,
        queued_ms=queued_ms,
        usage=usage_dict,
        budget=budget,
        workspace_revision=workspace_revision,
        platform_error=platform_error,
        events=events,
        model_latency_ms=model_latency,
        tool_latency_ms=tool_latency,
        estimated_cost=estimated,
        skills_enabled=variant_settings.review_agent_skills_enabled,
        context_optimization=variant_settings.review_agent_context_optimization,
        reviewer=reviewer,
    )


def _failure_reason(
    events: list[Any] | None,
    *,
    platform_error: str | None,
    outcome: str,
) -> str | None:
    if outcome == "success":
        return None
    if platform_error:
        return platform_error
    for event in reversed(events or []):
        etype = getattr(event, "type", None)
        message = getattr(event, "message", None) or ""
        if etype in {"run.failed", "model.error", "run.interrupted"} and message:
            return message
    return outcome


def _tool_latency_ms(events: list[Any]) -> float | None:
    total = 0.0
    found = False
    for event in events:
        payload = getattr(event, "payload", None) or {}
        if not isinstance(payload, dict):
            continue
        value = payload.get("duration_ms") or payload.get("tool_latency_ms")
        if value is None:
            continue
        found = True
        total += float(value)
    return total if found else None


def _result(
    task: EvalTask,
    *,
    variant: EvalVariant,
    tree_hash: str,
    code_commit: str | None,
    profile_id: str,
    run_id: str | None,
    run_status: str | None,
    hidden,
    elapsed_ms: float,
    queued_ms: float | None,
    usage: dict[str, Any] | None,
    budget: dict[str, Any] | None,
    workspace_revision: str | None,
    platform_error: str | None,
    events: list[Any] | None = None,
    model_latency_ms: float | None = None,
    tool_latency_ms: float | None = None,
    estimated_cost: float | None = None,
    skills_enabled: bool,
    context_optimization: bool,
    reviewer: str | None = None,
) -> EvalResult:
    outcome = classify_outcome(
        hidden=hidden,
        run_status=run_status,
        budget=budget,
        events=events,
        platform_error=platform_error,
    )
    return EvalResult(
        task_id=task.task_id,
        set=task.set,
        variant=variant,
        sample=task.sample,
        tree_hash=tree_hash,
        code_commit=code_commit,
        profile_id=profile_id,
        run_id=run_id,
        run_status=run_status,
        hidden_passed=hidden.passed,
        outcome=outcome,
        failure_reason=_failure_reason(events, platform_error=platform_error, outcome=outcome),
        usage=usage,
        budget=budget,
        queued_ms=queued_ms,
        elapsed_ms=elapsed_ms,
        model_latency_ms=model_latency_ms,
        tool_latency_ms=tool_latency_ms,
        estimated_cost=estimated_cost,
        workspace_revision=workspace_revision,
        hidden_exit_code=hidden.exit_code,
        reviewer=reviewer or reviewer_for(variant),
        skills_enabled=skills_enabled,
        context_optimization=context_optimization,
    )
