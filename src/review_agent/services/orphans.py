from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review_agent.harness.task_store import ToolExecutionRecord
from review_agent.services.executor import Executor
from review_agent.services.workspace_manager import RunWorkspace


@dataclass
class OrphanOutcome:
    stopped: list[str] = field(default_factory=list)
    already_exited: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)

    @property
    def has_unknown(self) -> bool:
        return bool(self.unknown)


def _metas_from_disk(workspace: RunWorkspace | None) -> list[dict[str, Any]]:
    if workspace is None:
        return []
    root = workspace.artifacts_root / "commands"
    if not root.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for meta in root.glob("*/execution.json"):
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            found.append(payload)
    return found


def stop_orphans(
    *,
    executor: Executor,
    records: list[ToolExecutionRecord],
    workspace: RunWorkspace | None,
) -> OrphanOutcome:
    """Confirm old commands/containers have exited before a new worker starts work."""
    outcome = OrphanOutcome()
    seen: set[str] = set()
    payloads: list[dict[str, Any]] = []
    for record in records:
        if record.result is not None:
            continue
        has_handle = bool(record.pid or record.pgid or record.container_name)
        if not has_handle:
            # Never started (e.g. waiting_approval). Crash recovery uses on-disk execution.json.
            continue
        payloads.append(
            {
                "execution_id": record.execution_id or record.call_id,
                "call_id": record.call_id,
                "pid": record.pid,
                "pgid": record.pgid,
                "container_name": record.container_name,
                "backend": "docker" if record.container_name else None,
                "status": record.execution_status,
            }
        )
    payloads.extend(_metas_from_disk(workspace))
    for payload in payloads:
        ident = str(payload.get("execution_id") or payload.get("call_id") or "")
        if not ident or ident in seen:
            continue
        status = str(payload.get("status") or "")
        if status in {"succeeded", "failed", "cancelled", "timed_out"}:
            continue
        seen.add(ident)
        result = executor.stop_persisted(
            pid=payload.get("pid"),
            pgid=payload.get("pgid"),
            container_name=payload.get("container_name"),
            backend=payload.get("backend"),
        )
        if result == "stopped":
            outcome.stopped.append(ident)
        elif result == "already_exited":
            outcome.already_exited.append(ident)
        else:
            outcome.unknown.append(ident)
    return outcome
