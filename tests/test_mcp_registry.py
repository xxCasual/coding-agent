from __future__ import annotations

from pathlib import Path

import pytest

from review_agent.harness.models import RegisteredTool, ReplayCategory, ToolResult, ToolRisk, ToolSpec
from review_agent.harness.tools import ToolRegistry
from review_agent.harness.tool_params import ListFilesParams


def _local_tool(name: str = "list_files") -> RegisteredTool:
    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description="list",
            risk=ToolRisk.READ_ONLY,
            input_schema=ListFilesParams.json_schema(),
            replay_category=ReplayCategory.REPEATABLE_READ,
        ),
        handler=lambda _args: ToolResult(success=True, summary="ok"),
        params_model=ListFilesParams,
    )


def test_register_rejects_duplicate_names() -> None:
    registry = ToolRegistry()
    registry.register(_local_tool())
    with pytest.raises(ValueError, match="duplicate tool name"):
        registry.register(_local_tool())


def test_json_schema_arguments_for_dynamic_tools() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="mcp__demo__echo",
                description="echo",
                risk=ToolRisk.READ_ONLY,
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
                replay_category=ReplayCategory.REPEATABLE_READ,
            ),
            handler=lambda args: ToolResult(success=True, summary=str(args["query"])),
            params_model=None,
        )
    )
    missing = registry.validate_arguments("mcp__demo__echo", {}, call_id="c1")
    assert isinstance(missing, ToolResult)
    assert missing.error_code == "invalid_arguments"
    ok = registry.validate_arguments("mcp__demo__echo", {"query": "hi"}, call_id="c1")
    assert not isinstance(ok, ToolResult)
    tool, payload = ok
    result = registry.execute(tool, payload, call_id="c1")
    assert result.success
    assert result.call_id == "c1"


def test_empty_json_schema_is_capability_missing() -> None:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            spec=ToolSpec(
                name="mcp__demo__empty",
                description="empty",
                risk=ToolRisk.WRITE_CONFIRM,
                input_schema={},
                replay_category=ReplayCategory.NON_REPLAYABLE,
            ),
            handler=lambda _args: ToolResult(success=True, summary="should not run"),
            params_model=None,
        )
    )
    outcome = registry.validate_arguments("mcp__demo__empty", {"x": 1}, call_id="c2")
    assert isinstance(outcome, ToolResult)
    assert outcome.error_code == "capability_missing"


def test_sdk_style_tool_schema_is_accepted() -> None:
    from review_agent.harness.mcp.schema import schema_capability_gap, validate_json_arguments

    schema = {
        "type": "object",
        "properties": {
            "contract_id": {"title": "Contract Id", "type": "string"},
            "method": {"title": "Method", "type": "string"},
            "path": {"title": "Path", "type": "string"},
        },
        "required": ["contract_id", "method", "path"],
        "title": "get_endpoint_contractArguments",
    }
    assert schema_capability_gap(schema) is None
    payload = validate_json_arguments(
        schema, {"contract_id": "items-v1", "method": "POST", "path": "/items"}
    )
    assert payload["contract_id"] == "items-v1"
