from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from review_agent.harness.models import ReplayCategory, ToolResult, ToolRisk
from review_agent.harness.tools import MAX_OUTPUT_CHARS

CONTRACT_TOOL_NAMES = frozenset(
    {
        "get_endpoint_contract",
        "compare_contract_versions",
        "validate_response_sample",
    }
)


def risk_for_discovered_tool(server_id: str, tool_name: str) -> tuple[ToolRisk, ReplayCategory]:
    if server_id == "api_contract" and tool_name in CONTRACT_TOOL_NAMES:
        return ToolRisk.READ_ONLY, ReplayCategory.REPEATABLE_READ
    return ToolRisk.WRITE_CONFIRM, ReplayCategory.NON_REPLAYABLE


def normalize_call_result(
    result: Any,
    *,
    call_id: str,
    risk: ToolRisk,
    artifacts_root: Path | None,
) -> ToolResult:
    is_error = bool(getattr(result, "is_error", False))
    structured = getattr(result, "structured_content", None)
    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            texts.append(str(text))
    if structured is not None:
        body = _json_dump(structured)
    elif texts:
        body = "\n".join(texts)
    else:
        body = ""
    summary = _summary_from_body(body, is_error=is_error)
    artifact_refs: list[str] = []
    stdout = body
    if len(body) > MAX_OUTPUT_CHARS and artifacts_root is not None:
        artifact_path = _write_artifact(artifacts_root, call_id, body)
        artifact_refs.append(str(artifact_path))
        stdout = body[:MAX_OUTPUT_CHARS] + "\n...[truncated; see artifact]...\n"
    return ToolResult(
        success=not is_error,
        summary=summary,
        stdout=stdout,
        call_id=call_id,
        error_code="execution_error" if is_error else None,
        artifact_refs=artifact_refs,
        risk_level=risk,
    )


def tool_result_for_failure(
    *,
    call_id: str,
    summary: str,
    error_code: str,
    risk: ToolRisk = ToolRisk.WRITE_CONFIRM,
) -> ToolResult:
    return ToolResult(
        success=False,
        summary=summary,
        call_id=call_id,
        error_code=error_code,
        risk_level=risk,
    )


def _summary_from_body(body: str, *, is_error: bool) -> str:
    first = body.strip().splitlines()[0] if body.strip() else ("MCP tool error" if is_error else "MCP tool completed")
    if len(first) > 240:
        return first[:237] + "..."
    return first


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _write_artifact(artifacts_root: Path, call_id: str, body: str) -> Path:
    directory = artifacts_root / "mcp"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{call_id or 'mcp-result'}.json"
    path.write_text(body, encoding="utf-8")
    return path
