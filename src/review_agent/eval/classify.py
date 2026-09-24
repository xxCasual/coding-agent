from __future__ import annotations

from typing import Any

from review_agent.eval.models import HiddenOutcome, Outcome

ENVIRONMENT_MODEL_CODES = {"missing_api_key", "stream_failed"}
PROTOCOL_MODEL_CODES = {"protocol", "invalid_tool_call", "invalid_tool_arguments", "parse_error"}


def classify_outcome(
    *,
    hidden: HiddenOutcome,
    run_status: str | None,
    budget: dict[str, Any] | None,
    events: list[Any] | None = None,
    platform_error: str | None = None,
) -> Outcome:
    if platform_error:
        return "platform"
    if hidden.could_not_run:
        return "environment"
    if hidden.passed:
        return "success"
    if _has_model_protocol_error(events or []):
        return "model_protocol"
    if _has_environment_model_error(events or []):
        return "environment"
    if _budget_exhausted(budget, run_status):
        return "budget"
    if (run_status or "") in {"failed", "cancelled", "interrupted", "needs_attention"}:
        return "implementation"
    return "verification"


def _budget_exhausted(budget: dict[str, Any] | None, run_status: str | None) -> bool:
    if not budget:
        return False
    step = budget.get("step_count")
    max_steps = budget.get("max_steps")
    if step is not None and max_steps is not None and int(step) >= int(max_steps):
        return True
    repairs = budget.get("repair_rounds")
    max_repairs = budget.get("max_verification_repairs")
    if repairs is not None and max_repairs is not None and int(repairs) >= int(max_repairs):
        return True
    return (run_status or "") == "failed" and bool(budget.get("exhausted"))


def _has_model_protocol_error(events: list[Any]) -> bool:
    for event in events:
        etype = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
        payload = getattr(event, "payload", None) or (event.get("payload") if isinstance(event, dict) else {}) or {}
        code = payload.get("error_code") if isinstance(payload, dict) else None
        if etype in {"model.error", "mcp.error"} and code in PROTOCOL_MODEL_CODES:
            return True
    return False


def _has_environment_model_error(events: list[Any]) -> bool:
    for event in events:
        etype = getattr(event, "type", None) or (event.get("type") if isinstance(event, dict) else None)
        payload = getattr(event, "payload", None) or (event.get("payload") if isinstance(event, dict) else {}) or {}
        code = payload.get("error_code") if isinstance(payload, dict) else None
        if code in ENVIRONMENT_MODEL_CODES:
            return True
        message = getattr(event, "message", None) or (event.get("message") if isinstance(event, dict) else "") or ""
        text = f"{etype} {message}".lower()
        if "model error:" in text or etype == "model.error":
            if any(
                token in text
                for token in ("connection error", "not configured", "stream failed", "connection refused")
            ):
                return True
    return False
