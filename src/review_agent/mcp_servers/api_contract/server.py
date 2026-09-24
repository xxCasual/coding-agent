from __future__ import annotations

import logging
import sys
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from review_agent.mcp_servers.api_contract.catalog import ContractError
from review_agent.mcp_servers.api_contract.logic import (
    compare_contract_versions as compare_contract_versions_fn,
)
from review_agent.mcp_servers.api_contract.logic import (
    get_endpoint_contract as get_endpoint_contract_fn,
)
from review_agent.mcp_servers.api_contract.logic import (
    validate_response_sample as validate_response_sample_fn,
)

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
mcp = MCPServer(
    "api-contract",
    instructions="Look up registered OpenAPI 3.1 contracts by contract_id. Do not pass file paths or URLs.",
)


def _tool_error(exc: ContractError) -> ToolError:
    return ToolError(f"{exc.code}: {exc}")


@mcp.tool(description="Return one registered endpoint contract: parameters, body, responses, version and hash.")
def get_endpoint_contract(contract_id: str, method: str, path: str) -> dict[str, Any]:
    try:
        return get_endpoint_contract_fn(contract_id, method, path)
    except ContractError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(description="Compare two registered contracts and list detected request/response changes plus unsupported items.")
def compare_contract_versions(old_contract_id: str, new_contract_id: str) -> dict[str, Any]:
    try:
        return compare_contract_versions_fn(old_contract_id, new_contract_id)
    except ContractError as exc:
        raise _tool_error(exc) from exc


@mcp.tool(description="Validate a response sample against a registered endpoint. Returns valid, invalid, or unsupported.")
def validate_response_sample(
    contract_id: str,
    method: str,
    path: str,
    status_code: int,
    sample: dict[str, Any],
) -> dict[str, Any]:
    try:
        return validate_response_sample_fn(contract_id, method, path, status_code, sample)
    except ContractError as exc:
        raise _tool_error(exc) from exc


def main() -> None:
    mcp.run(transport="stdio")
