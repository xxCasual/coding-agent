from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Outcome = Literal[
    "success",
    "implementation",
    "verification",
    "budget",
    "environment",
    "model_protocol",
    "platform",
]
EvalVariant = Literal["baseline", "full"]
TaskSet = Literal["dev", "heldout"]
TaskCategory = Literal["fix", "feature", "contract"]


@dataclass(frozen=True)
class EvalTask:
    task_id: str
    set: TaskSet
    category: TaskCategory
    sample: str
    tree_hash: str
    requirement: str
    allowed_paths: list[str]
    budget: dict[str, Any]
    hidden_script: Path
    reviewer_hard: bool = False

    @classmethod
    def from_dict(cls, payload: dict[str, Any], *, repo_root: Path) -> EvalTask:
        hidden = payload.get("hidden_acceptance") or {}
        script = hidden.get("script") or hidden.get("path")
        if not script:
            raise ValueError(f"task {payload.get('task_id')} missing hidden_acceptance.script")
        budget = dict(payload.get("budget") or {})
        budget.setdefault("max_steps", 24)
        budget.setdefault("max_verification_repairs", 2)
        return cls(
            task_id=str(payload["task_id"]),
            set=payload.get("set") or "dev",
            category=payload["category"],
            sample=str(payload["sample"]),
            tree_hash=str(payload.get("tree_hash") or ""),
            requirement=str(payload["requirement"]),
            allowed_paths=list(payload.get("allowed_paths") or []),
            budget=budget,
            hidden_script=(repo_root / script).resolve(),
            reviewer_hard=bool(payload.get("reviewer_hard")),
        )


@dataclass
class HiddenOutcome:
    passed: bool
    exit_code: int | None
    could_not_run: bool
    stdout: str = ""
    stderr: str = ""


@dataclass
class EvalResult:
    task_id: str
    set: str
    variant: EvalVariant
    sample: str
    tree_hash: str
    code_commit: str | None
    profile_id: str
    run_id: str | None
    run_status: str | None
    hidden_passed: bool
    outcome: Outcome
    failure_reason: str | None
    usage: dict[str, Any] | None
    budget: dict[str, Any] | None
    queued_ms: float | None
    elapsed_ms: float
    model_latency_ms: float | None
    tool_latency_ms: float | None
    estimated_cost: float | None
    workspace_revision: str | None
    hidden_exit_code: int | None
    reviewer: str
    skills_enabled: bool
    context_optimization: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "set": self.set,
            "variant": self.variant,
            "sample": self.sample,
            "tree_hash": self.tree_hash,
            "code_commit": self.code_commit,
            "profile_id": self.profile_id,
            "run_id": self.run_id,
            "run_status": self.run_status,
            "hidden_passed": self.hidden_passed,
            "outcome": self.outcome,
            "failure_reason": self.failure_reason,
            "usage": self.usage,
            "budget": self.budget,
            "queued_ms": self.queued_ms,
            "elapsed_ms": self.elapsed_ms,
            "model_latency_ms": self.model_latency_ms,
            "tool_latency_ms": self.tool_latency_ms,
            "estimated_cost": self.estimated_cost,
            "workspace_revision": self.workspace_revision,
            "hidden_exit_code": self.hidden_exit_code,
            "reviewer": self.reviewer,
            "skills_enabled": self.skills_enabled,
            "context_optimization": self.context_optimization,
        }
