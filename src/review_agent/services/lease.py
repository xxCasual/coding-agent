from __future__ import annotations

from datetime import datetime, timedelta, timezone

from review_agent.harness.models import RunStatus
from review_agent.harness.task_store import ApprovalRecord, RunRecord


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lease_until_iso(seconds: int, *, now: datetime | None = None) -> str:
    stamp = (now or utcnow()) + timedelta(seconds=max(1, seconds))
    return stamp.isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def lease_expired(lease_until: str | None, *, now: datetime | None = None) -> bool:
    parsed = parse_iso(lease_until)
    if parsed is None:
        return True
    return parsed <= (now or utcnow())


def run_is_claimable(
    run: RunRecord,
    *,
    approvals: list[ApprovalRecord] | None = None,
) -> bool:
    if run.status == RunStatus.QUEUED:
        return True
    if run.status == RunStatus.INTERRUPTED:
        return True
    if run.status == RunStatus.RUNNING:
        return lease_expired(run.lease_until)
    if run.status == RunStatus.WAITING_APPROVAL:
        records = approvals or []
        pending = [item for item in records if item.decision is None]
        decided = [item for item in records if item.decision is not None]
        return bool(decided) and not pending
    if run.status == RunStatus.NEEDS_ATTENTION:
        action = (run.attention or {}).get("action")
        return action in {"accept_and_continue", "end_task"}
    return False
