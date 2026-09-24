from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from review_agent.harness.models import ReplayCategory, RunStatus, ToolResult, ToolRisk
from review_agent.harness.task_store import TaskStore, ToolExecutionRecord
from review_agent.harness.workspace_revision import compute_workspace_revision, file_content_hash
from review_agent.services.patch_apply import PatchError, plan_patch

_FINISHED_COMMAND = {"succeeded", "failed", "cancelled", "timed_out"}


@dataclass
class ReconcileDecision:
    status: Literal["ok", "needs_attention"]
    reason: str = ""
    call_id: str | None = None
    affected_files: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)
    reconciled: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    def as_attention(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "call_id": self.call_id,
            "affected_files": list(self.affected_files),
            **self.payload,
        }


def replay_category_of(record: ToolExecutionRecord) -> ReplayCategory | None:
    raw = record.replay_category
    if not raw:
        return None
    try:
        return ReplayCategory(raw)
    except ValueError:
        return None


def _has_process_handle(record: ToolExecutionRecord) -> bool:
    return bool(record.pid or record.pgid or record.container_name)


def _awaiting_execution(record: ToolExecutionRecord) -> bool:
    return (record.execution_status in {"pending", "awaiting_approval"}
            and not record.started_at and not _has_process_handle(record))


def incomplete_unknown(record: ToolExecutionRecord) -> bool:
    if record.result is not None or _awaiting_execution(record):
        return False
    if replay_category_of(record) is ReplayCategory.REPEATABLE_READ:
        return False
    return True


def unknown_side_effects(store: TaskStore, run_id: str) -> list[ToolExecutionRecord]:
    return [item for item in store.list_tool_executions(run_id) if incomplete_unknown(item)]


def reconcile_run(
    *,
    store: TaskStore,
    workspace_root: Path,
    run_id: str,
) -> ReconcileDecision:
    """Compare checkpoint/tool ledger with disk before producing new side effects."""
    records = store.list_tool_executions(run_id)
    reused: list[str] = []
    reconciled: list[str] = []
    for record in records:
        if record.result is not None:
            reused.append(record.call_id)
            continue
        decision = _reconcile_incomplete(store, workspace_root, record)
        if decision is None:
            continue
        if decision.status == "ok":
            reconciled.extend(decision.reconciled)
            continue
        return decision
    return ReconcileDecision(status="ok", reused=reused, reconciled=reconciled)


def _reconcile_incomplete(
    store: TaskStore,
    workspace_root: Path,
    record: ToolExecutionRecord,
) -> ReconcileDecision | None:
    if _awaiting_execution(record):
        return None
    replay = replay_category_of(record)
    name = record.tool_call.name
    if replay is ReplayCategory.REPEATABLE_READ:
        return None
    if replay is ReplayCategory.PATCH_CHECKABLE:
        return _reconcile_patch(store, workspace_root, record)
    if _has_process_handle(record):
        return ReconcileDecision(
            status="needs_attention",
            reason="Command side effects are unknown after worker restart.",
            call_id=record.call_id,
        )
    if replay is ReplayCategory.CONTROLLED_VERIFY:
        if record.execution_status in _FINISHED_COMMAND:
            return ReconcileDecision(
                status="needs_attention",
                reason="Verification command finished without a persisted ToolResult.",
                call_id=record.call_id,
            )
        return None
    return ReconcileDecision(
        status="needs_attention",
        reason=f"Unknown or non-replayable call {name} has no complete result.",
        call_id=record.call_id,
    )


def _reconcile_patch(
    store: TaskStore,
    workspace_root: Path,
    record: ToolExecutionRecord,
) -> ReconcileDecision | None:
    patch = str((record.tool_call.arguments or {}).get("patch") or "")
    if not patch:
        return ReconcileDecision(
            status="needs_attention",
            reason="apply_patch record is missing patch text",
            call_id=record.call_id,
        )
    status = _patch_disk_status(workspace_root, patch)
    if status == "applied":
        planned = plan_patch(workspace_root, patch)
        store.complete_tool_execution(
            record.call_id,
            ToolResult(
                success=True,
                summary="Reconciled: patch already applied (after hashes match).",
                call_id=record.call_id,
                changed_files=[item.path for item in planned.files],
                risk_level=ToolRisk.WRITE_CONFIRM,
            ),
            execution_meta={
                "patch_hash": planned.patch_hash,
                "status": "succeeded",
                "already_applied": True,
            },
        )
        return ReconcileDecision(status="ok", reconciled=[record.call_id])
    if status in {"partial", "uncheckable"}:
        return ReconcileDecision(
            status="needs_attention",
            reason="Patch is partially applied or not checkable; files must be reconciled.",
            call_id=record.call_id,
        )
    return None


def mark_needs_attention(store: TaskStore, run_id: str, decision: ReconcileDecision) -> None:
    store.update_run(
        run_id,
        status=RunStatus.NEEDS_ATTENTION,
        attention=decision.as_attention(),
    )


def current_revision(workspace_root: Path) -> str:
    return compute_workspace_revision(workspace_root)


def _patch_disk_status(workspace_root: Path, patch: str) -> str:
    try:
        planned = plan_patch(workspace_root, patch)
    except PatchError:
        return "uncheckable"
    after = 0
    before = 0
    other = 0
    for item in planned.files:
        path = workspace_root / item.path
        if item.is_delete:
            if not path.exists():
                after += 1
            elif file_content_hash(path) == item.before_hash:
                before += 1
            else:
                other += 1
            continue
        actual = file_content_hash(path if path.is_file() else None)
        if item.checkable and item.expected_after_hash and actual == item.expected_after_hash:
            after += 1
        elif actual == item.before_hash:
            before += 1
        else:
            other += 1
    if after == len(planned.files):
        return "applied"
    if before == len(planned.files) and other == 0:
        return "not_applied"
    return "partial"
