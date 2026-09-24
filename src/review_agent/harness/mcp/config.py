from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SERVER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")


@dataclass(frozen=True)
class McpServerConfig:
    server_id: str
    command: list[str]
    env_allowlist: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    env: dict[str, str] = field(default_factory=dict)


def parse_mcp_servers(raw: str, *, default_timeout: float = 30.0) -> list[McpServerConfig]:
    """Parse REVIEW_AGENT_MCP_SERVERS JSON. Empty means no servers (explicit config only)."""
    text = (raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"REVIEW_AGENT_MCP_SERVERS is not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError("REVIEW_AGENT_MCP_SERVERS must be a JSON array")
    servers: list[McpServerConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise ValueError(f"MCP server config {index} must be an object")
        server_id = str(item.get("server_id") or "").strip()
        if not SERVER_ID_RE.match(server_id):
            raise ValueError(f"invalid MCP server_id at index {index}: {server_id!r}")
        if server_id in seen:
            raise ValueError(f"duplicate MCP server_id: {server_id}")
        seen.add(server_id)
        command = item.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(part, str) and part for part in command):
            raise ValueError(f"MCP server {server_id} command must be a non-empty argv list")
        allowlist = item.get("env_allowlist") or []
        if not isinstance(allowlist, list) or not all(isinstance(name, str) for name in allowlist):
            raise ValueError(f"MCP server {server_id} env_allowlist must be a list of names")
        extra_env = item.get("env") or {}
        if not isinstance(extra_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in extra_env.items()
        ):
            raise ValueError(f"MCP server {server_id} env must be a string mapping")
        timeout = item.get("timeout_seconds", default_timeout)
        try:
            timeout_value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"MCP server {server_id} timeout_seconds is invalid") from exc
        if timeout_value <= 0:
            raise ValueError(f"MCP server {server_id} timeout_seconds must be positive")
        servers.append(
            McpServerConfig(
                server_id=server_id,
                command=list(command),
                env_allowlist=tuple(str(name) for name in allowlist),
                timeout_seconds=timeout_value,
                env=dict(extra_env),
            )
        )
    return servers


def namespaced_tool_name(server_id: str, tool_name: str) -> str:
    return f"mcp__{server_id}__{tool_name}"
