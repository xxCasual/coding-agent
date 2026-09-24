from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from review_agent.harness.models import (
    AgentEvent,
    Message,
    RunStatus,
    ToolCall,
    ToolResult,
    ToolRisk,
    VerificationRecord,
    VerificationStatus,
)
from review_agent.harness.task_store import AcceptanceSpec, RunRecord


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    value = dt or utcnow()
    return value.isoformat(timespec="seconds")


def parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def call_hash(tool_call: ToolCall) -> str:
    payload = {"name": tool_call.name, "arguments": tool_call.arguments or {}}
    return hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()


def file_sha256(path_bytes: bytes) -> str:
    return hashlib.sha256(path_bytes).hexdigest()


def acceptance_to_json(spec: AcceptanceSpec) -> dict[str, Any]:
    return {"mode": spec.mode, "checks": list(spec.checks), "description": spec.description}


def acceptance_from_json(raw: dict[str, Any] | None) -> AcceptanceSpec:
    data = raw or {}
    return AcceptanceSpec(
        mode=str(data.get("mode") or "unverified"),
        checks=list(data.get("checks") or []),
        description=str(data.get("description") or ""),
    )


def tool_call_to_json(call: ToolCall) -> dict[str, Any]:
    return call.model_dump(mode="json")


def tool_call_from_json(raw: dict[str, Any] | None) -> ToolCall:
    data = raw or {}
    return ToolCall.model_validate(
        {
            "name": str(data.get("name") or ""),
            "arguments": dict(data.get("arguments") or {}),
            "call_id": str(data.get("call_id") or ""),
            "provider_call_id": data.get("provider_call_id"),
        }
    )


def verification_to_json(record: VerificationRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    dumped = record.model_dump(mode="json")
    dumped["status"] = record.status.value
    dumped["evidence_refs"] = list(record.evidence_refs)
    return dumped


def verification_from_json(raw: dict[str, Any] | None) -> VerificationRecord | None:
    if not raw:
        return None
    status_raw = raw.get("status") or "unverified"
    try:
        status = VerificationStatus(status_raw)
    except ValueError:
        status = VerificationStatus.UNVERIFIED
    return VerificationRecord.model_validate(
        {
            "status": status,
            "command": raw.get("command"),
            "exit_code": raw.get("exit_code"),
            "workspace_revision": raw.get("workspace_revision"),
            "evidence_refs": list(raw.get("evidence_refs") or []),
        }
    )


def tool_result_to_json(result: ToolResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    dumped = result.model_dump(mode="json")
    dumped["risk_level"] = result.risk_level.value
    dumped["changed_files"] = list(result.changed_files)
    dumped["artifact_refs"] = list(result.artifact_refs)
    dumped["verification"] = verification_to_json(result.verification)
    return dumped


def tool_result_from_json(raw: dict[str, Any] | None) -> ToolResult | None:
    if not raw:
        return None
    risk_raw = raw.get("risk_level") or "read_only"
    try:
        risk = ToolRisk(risk_raw)
    except ValueError:
        risk = ToolRisk.READ_ONLY
    return ToolResult.model_validate(
        {
            "success": bool(raw.get("success")),
            "summary": str(raw.get("summary") or ""),
            "stdout": str(raw.get("stdout") or ""),
            "stderr": str(raw.get("stderr") or ""),
            "changed_files": list(raw.get("changed_files") or []),
            "verification_hint": str(raw.get("verification_hint") or ""),
            "risk_level": risk,
            "call_id": str(raw.get("call_id") or ""),
            "error_code": raw.get("error_code"),
            "artifact_refs": list(raw.get("artifact_refs") or []),
            "verification": verification_from_json(raw.get("verification")),
        }
    )


def message_to_json(message: Message) -> dict[str, Any]:
    dumped = message.model_dump(mode="json")
    dumped["tool_calls"] = (
        [tool_call_to_json(item) for item in message.tool_calls] if message.tool_calls else None
    )
    return dumped


def message_from_parts(
    *,
    role: str,
    content: str,
    message_id: str,
    session_id: str,
    run_id: str | None,
    tool_calls: list | None,
    provider_call_id: str | None,
) -> Message:
    calls = None
    if tool_calls:
        calls = [tool_call_from_json(item) if isinstance(item, dict) else item for item in tool_calls]
    return Message.model_validate(
        {
            "role": role,
            "content": content,
            "message_id": message_id,
            "session_id": session_id,
            "run_id": run_id or "",
            "tool_calls": calls,
            "provider_call_id": provider_call_id,
        }
    )


def public_event_payload(event: AgentEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})
    call_id = payload.get("call_id")
    if event.tool_call is not None:
        call_id = call_id or event.tool_call.call_id
        payload.setdefault("tool_name", event.tool_call.name)
        payload.setdefault("call_id", event.tool_call.call_id)
    if event.tool_result is not None:
        call_id = call_id or event.tool_result.call_id
        payload.setdefault("call_id", event.tool_result.call_id)
        payload.setdefault("success", event.tool_result.success)
        payload.setdefault("summary", event.tool_result.summary)
        if event.tool_result.error_code:
            payload.setdefault("error_code", event.tool_result.error_code)
        if event.tool_result.artifact_refs:
            payload.setdefault("artifact_refs", list(event.tool_result.artifact_refs))
    if event.approval_request is not None:
        payload.setdefault("tool_name", event.approval_request.tool_name)
        payload.setdefault("reason", event.approval_request.reason)
        payload.setdefault("workspace_revision", event.approval_request.workspace_revision)
        payload.setdefault("patch_hash", event.approval_request.patch_hash)
        args = event.approval_request.arguments or {}
        payload.setdefault("param_summary", ", ".join(sorted(args.keys())))
    if "arguments" in payload:
        payload.pop("arguments", None)
    return payload, call_id if isinstance(call_id, str) else None


def event_from_row(
    *,
    event_type: str,
    message: str,
    run_id: str,
    seq: int,
    payload: dict[str, Any] | None,
) -> AgentEvent:
    return AgentEvent(
        type=event_type,
        message=message,
        run_id=run_id,
        seq=seq,
        payload=dict(payload or {}),
    )


def run_from_row(
    *,
    run_id: str,
    session_id: str,
    status: str,
    requirement: str,
    profile_id: str,
    acceptance: dict[str, Any] | None,
    idempotency_key: str | None,
    cancel_requested: bool,
    workspace_revision: str | None,
    verification: dict[str, Any] | None,
    dispatch_pending: bool,
    budget: dict[str, Any] | None,
    usage: dict[str, Any] | None,
    workspace_snapshot: dict[str, Any] | None,
    created_at: str,
    updated_at: str,
    owner_id: str | None = None,
    lease_until: str | None = None,
    heartbeat_at: str | None = None,
    attention: dict[str, Any] | None = None,
    review: dict[str, Any] | None = None,
    task_mode: str = "develop",
    review_target: dict[str, Any] | None = None,
) -> RunRecord:
    from review_agent.harness.review_target import ReviewTarget
    return RunRecord(
        run_id=run_id,
        session_id=session_id,
        status=RunStatus(status),
        requirement=requirement,
        profile_id=profile_id,
        acceptance=acceptance_from_json(acceptance),
        idempotency_key=idempotency_key,
        cancel_requested=cancel_requested,
        workspace_revision=workspace_revision,
        verification=verification,
        created_at=created_at,
        updated_at=updated_at,
        dispatch_pending=dispatch_pending,
        budget=budget,
        usage=usage,
        workspace_snapshot=workspace_snapshot,
        owner_id=owner_id,
        lease_until=lease_until,
        heartbeat_at=heartbeat_at,
        attention=attention,
        review=review,
        task_mode=task_mode,
        review_target=ReviewTarget.model_validate(review_target) if review_target else None,
    )


def artifact_kind_for(path_name: str) -> str:
    lower = path_name.lower()
    if lower.endswith(".patch"):
        return "patch"
    if lower.endswith("stdout.log"):
        return "command_stdout"
    if lower.endswith("stderr.log"):
        return "command_stderr"
    if "mcp" in lower:
        return "mcp_output"
    return "file"


def merge_usage(existing: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any] | None:
    if incoming is None:
        return existing
    if existing is None:
        return dict(incoming)
    merged = dict(existing)
    for key in ("input_tokens", "output_tokens"):
        left = existing.get(key)
        right = incoming.get(key)
        if left is None or right is None:
            merged[key] = None
        else:
            merged[key] = int(left) + int(right)
    for key in ("model_latency_ms", "estimated_cost"):
        left, right = existing.get(key), incoming.get(key)
        merged[key] = left + right if left is not None and right is not None else None
    for key in ("profile_id", "price_config_id", "price_as_of"):
        if incoming.get(key) is not None:
            merged[key] = incoming[key]
    return merged
