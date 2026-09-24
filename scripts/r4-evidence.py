"""Bounded R4 evidence using existing TaskService, runtime and shared budget ledger."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path

from review_agent.config import Settings
from review_agent.eval.acceptance import run_hidden_acceptance
from review_agent.eval.snapshot import materialize_sample
from review_agent.harness.budget import BudgetLedger
from review_agent.harness.review_target import ReviewTarget
from review_agent.harness.task_store import AcceptanceSpec, InMemoryTaskStore
from review_agent.harness.workspace_revision import compute_workspace_revision
from review_agent.services.dispatch import NoOpPublisher
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager

ROOT = Path(__file__).resolve().parents[1]


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=lambda value: value.model_dump(mode="json") if hasattr(value, "model_dump") else str(value)) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--phase", choices=["development", "pair"], required=True)
    parser.add_argument("--suite", type=Path, default=ROOT / "eval/r4")
    args = parser.parse_args()
    suite = args.suite.resolve()
    tasks = json.loads((suite / "tasks.json").read_text())
    if args.out.exists():
        raise SystemExit("Use a new evidence directory; previous attempts must be retained.")
    settings = Settings(deepseek_model="deepseek-flash", deepseek_base_url="https://api.deepseek.com",
        review_agent_budget_path=str(args.budget.resolve()), review_agent_database_url="", review_agent_celery_broker="",
        review_agent_executor_backend="host", review_agent_approval_mode="confirm", review_agent_llm_max_retries=0,
        review_agent_llm_timeout_seconds=90, review_agent_agent_max_steps=24, review_agent_reviewer_max_steps=6,
        review_agent_price_input_per_1m=2, review_agent_price_output_per_1m=8, review_agent_price_as_of="2026-09-23",
        review_agent_skills_enabled=True, review_agent_context_optimization=True, review_agent_memory_enabled=False,
        review_agent_mcp_servers="")
    if not settings.deepseek_api_key:
        raise SystemExit("Configured provider key unavailable; no request sent.")
    # Keep the configured key in the model settings only. Project commands inherit no credentials/proxies.
    allowed = {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "CONDA_PREFIX", "CONDA_DEFAULT_ENV", "SYSTEMROOT"}
    for key in list(os.environ):
        if key not in allowed:
            os.environ.pop(key)
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    args.out.mkdir(parents=True)
    save(args.out / "budget-before.json", json.loads(args.budget.read_text()))
    files = [*sorted((ROOT / "src/review_agent").rglob("*.py")), ROOT / "scripts/r4-evidence.py", *sorted(suite.glob("*.py")), suite / "tasks.json", ROOT / "eval/r4/hidden.py"]
    save(args.out / "manifest.json", {"phase": args.phase, "suite": str(suite.relative_to(ROOT)), "tasks": tasks, "executor": "host", "store": "InMemoryTaskStore",
        "profile": asdict(settings.get_model_profile()), "max_steps": 24, "reviewer_max_steps": 6,
        "skills": True, "context_optimization": True, "memory": False, "mcp": [], "thinking": "disabled",
        "max_output_tokens": BudgetLedger.MAX_OUTPUT, "code_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}})
    results = []

    def setup(name):
        folder = args.out / name
        folder.mkdir()
        original = folder / "original"
        original_revision = materialize_sample(ROOT / "eval/samples/r4-api", original)
        configured = settings.model_copy(update={"review_agent_data_root": str((folder / "data").resolve())})
        manager = WorkspaceManager(configured.review_agent_data_root)
        manager.register(name, original, display_name=name)
        service = TaskService(InMemoryTaskStore(), manager, configured, publisher=NoOpPublisher())
        return folder, original, original_revision, service, service.create_session(name).session_id

    def execute(context, case, *, reviewer="default", mode="develop"):
        folder, original, initial, service, sid = context
        before = compute_workspace_revision(original)
        started = time.perf_counter()
        run = service.create_run(sid, tasks[case], idempotency_key=case, reviewer=reviewer, task_mode=mode,
            review_target=ReviewTarget(kind="workspace_changes") if mode == "review" else None,
            acceptance=AcceptanceSpec(mode="commands", checks=[[sys.executable, "-m", "pytest", "-q"]]) if mode == "develop" else None)
        save(folder / f"{case}-input.json", asdict(run))
        print(f"START {folder.name}/{case} run={run.run_id}", flush=True)
        def approve(request):
            if request.tool_name == "apply_patch":
                return True
            argv = request.arguments.get("argv") or request.arguments.get("command") or []
            return isinstance(argv, list) and len(argv) >= 3 and Path(argv[0]).name in {"python", "python3", "python3.11"} and argv[1:3] == ["-m", "pytest"]
        error = None
        try:
            service.run_once(run.run_id, approver=approve)
        except Exception as exc:
            error = type(exc).__name__  # Do not expose provider headers or settings.
        current = service.store.get_run(run.run_id)
        prepared = service.workspace_manager.get_run(run.run_id)
        hidden = None
        if mode == "develop" and prepared:
            os.environ["R4_CASE"] = case
            hidden = asdict(run_hidden_acceptance(suite / "hidden.py", prepared.source_root))
        patch_path = None
        if mode == "develop" and prepared:
            patch_path = service.workspace_manager.compute_delivery_patch(run.run_id)
        changed = compute_workspace_revision(original) != before
        readonly_unchanged = mode == "develop" or (prepared is not None and compute_workspace_revision(prepared.source_root) == current.workspace_snapshot["source_revision"])
        record = {"case": case, "reviewer": reviewer, "run": asdict(current), "elapsed_seconds": time.perf_counter()-started,
            "template_revision": initial, "original_input_revision": before, "original_unchanged": not changed, "readonly_unchanged": readonly_unchanged,
            "hidden": hidden, "delivery_patch": str(patch_path) if patch_path else None, "error_type": error, "evidence_dir": str(folder.resolve())}
        save(folder / f"{case}-result.json", record)
        save(folder / f"{case}-events.json", [e.model_dump(mode="json") for e in service.store.list_events(run.run_id)])
        save(folder / f"{case}-tools.json", [asdict(item) for item in service.store.list_tool_executions(run.run_id)])
        messages = service.store.list_messages(sid)
        save(folder / f"{case}-messages.json", [m.model_dump(mode="json") for m in messages])
        results.append(record)
        save(args.out / "results.json", results)
        save(args.out / "budget-after.json", json.loads(args.budget.read_text()))
        print(f"DONE {folder.name}/{case} status={current.status.value} hidden={hidden and hidden['passed']} original_unchanged={not changed}", flush=True)
        return current.status.value == "succeeded" and not changed and readonly_unchanged and (hidden is None or hidden["passed"])

    if args.phase == "development":
        dev = setup("development")
        if not execute(dev, "development"):
            return 1
        review = setup("review-existing")
        original = review[1]
        (original / "app.py").write_text((original / "app.py").read_text().replace('"ok"', '"broken"'))
        if not execute(review, "review", mode="review"):
            return 1
        followed = execute(dev, "followup")
        planned = execute(dev, "plan", mode="plan", reviewer="off")
        if not (followed and planned):
            return 1
    else:
        # No shared memory, modified source or model state between the two arms.
        on = execute(setup("reviewer-on"), "pair")
        off = execute(setup("reviewer-off"), "pair", reviewer="off")
        if not (on and off):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
