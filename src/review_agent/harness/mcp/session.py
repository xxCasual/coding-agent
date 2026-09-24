from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from review_agent.config import Settings
from review_agent.harness.mcp.config import McpServerConfig, namespaced_tool_name, parse_mcp_servers
from review_agent.harness.mcp.normalize import (
    normalize_call_result,
    risk_for_discovered_tool,
    tool_result_for_failure,
)
from review_agent.harness.mcp.schema import schema_capability_gap
from review_agent.harness.models import RegisteredTool, ToolResult, ToolSpec
from review_agent.harness.tools import ToolRegistry

logger = logging.getLogger(__name__)

EmitFn = Callable[[str, str, dict[str, Any]], None]

_SECRET_ENV_NAMES = frozenset(
    {
        "DEEPSEEK_API_KEY",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "REVIEW_AGENT_ALT_API_KEY",
    }
)


class McpSessionManager:
    """Run-scoped stdio MCP sessions using the official SDK Client."""

    def __init__(
        self,
        servers: list[McpServerConfig],
        *,
        artifacts_root: Path | None = None,
        pythonpath: str | None = None,
        contract_root: Path | None = None,
    ) -> None:
        self.servers = servers
        self.artifacts_root = Path(artifacts_root).resolve() if artifacts_root else None
        self.pythonpath = pythonpath
        self.contract_root = contract_root
        self._clients: dict[str, Any] = {}
        self._tool_meta: dict[str, tuple[str, str, Any]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        self._closed = False
        self._close_lock = asyncio.Lock()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        artifacts_root: Path | None = None,
    ) -> McpSessionManager:
        servers = parse_mcp_servers(
            settings.review_agent_mcp_servers,
            default_timeout=settings.review_agent_mcp_timeout_seconds,
        )
        return cls(servers, artifacts_root=artifacts_root)

    def request_close(self) -> None:
        self._closing = True
        if self._loop is None or not self._clients:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.close(), self._loop)
        except RuntimeError:
            logger.warning("MCP close could not be scheduled on the run loop")

    async def start(self, registry: ToolRegistry, *, emit: EmitFn | None = None) -> None:
        if not self.servers:
            return
        self._loop = asyncio.get_running_loop()
        from mcp import Client, StdioServerParameters

        for config in self.servers:
            if self._closing:
                break
            env = self._child_env(config)
            params = StdioServerParameters(
                command=config.command[0],
                args=list(config.command[1:]),
                env=env,
            )
            client = Client(params, read_timeout_seconds=config.timeout_seconds)
            try:
                await client.__aenter__()
            except Exception as exc:
                try:
                    await client.__aexit__(None, None, None)
                except Exception:
                    logger.warning(
                        "MCP server %s failed during connect; cleanup also failed",
                        config.server_id,
                        exc_info=True,
                    )
                if emit:
                    emit(
                        "mcp.error",
                        f"failed to start MCP server {config.server_id}: {exc}",
                        {"server_id": config.server_id, "error_code": "disconnected"},
                    )
                continue
            self._clients[config.server_id] = client
            if emit:
                emit(
                    "mcp.connected",
                    f"connected to MCP server {config.server_id}",
                    {
                        "server_id": config.server_id,
                        "protocol_version": getattr(client, "protocol_version", None),
                    },
                )
            try:
                listed = await client.list_tools()
            except Exception as exc:
                if emit:
                    emit(
                        "mcp.error",
                        f"tool discovery failed for {config.server_id}: {exc}",
                        {"server_id": config.server_id, "error_code": "disconnected"},
                    )
                await self._close_client(config.server_id)
                continue
            discovered: list[str] = []
            skipped: list[dict[str, str]] = []
            for tool in listed.tools:
                mapped = namespaced_tool_name(config.server_id, tool.name)
                schema = getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {}
                gap = schema_capability_gap(schema)
                if gap:
                    skipped.append({"name": mapped, "reason": gap})
                    continue
                try:
                    registry.register(
                        RegisteredTool(
                            spec=ToolSpec(
                                name=mapped,
                                description=tool.description or f"MCP tool {tool.name}",
                                risk=risk_for_discovered_tool(config.server_id, tool.name)[0],
                                input_schema=schema,
                                replay_category=risk_for_discovered_tool(config.server_id, tool.name)[1],
                            ),
                            handler=self._make_handler(config, tool.name),
                            params_model=None,
                        )
                    )
                except ValueError as exc:
                    skipped.append({"name": mapped, "reason": str(exc)})
                    continue
                self._tool_meta[mapped] = (config.server_id, tool.name, schema)
                discovered.append(mapped)
            if emit:
                emit(
                    "mcp.tools_discovered",
                    f"discovered {len(discovered)} tools from {config.server_id}",
                    {
                        "server_id": config.server_id,
                        "tools": discovered,
                        "skipped": skipped,
                    },
                )

    async def close(self) -> None:
        self._closing = True
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            server_ids = list(self._clients)
            for server_id in server_ids:
                await self._close_client(server_id)
            self._clients.clear()
            self._tool_meta.clear()

    async def _close_client(self, server_id: str) -> None:
        client = self._clients.pop(server_id, None)
        if client is None:
            return
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            logger.warning("MCP server %s did not close cleanly", server_id, exc_info=True)

    def _make_handler(self, config: McpServerConfig, tool_name: str):
        risk, _replay = risk_for_discovered_tool(config.server_id, tool_name)

        async def handler(arguments: dict[str, Any]) -> ToolResult:
            client = self._clients.get(config.server_id)
            if client is None:
                return tool_result_for_failure(
                    call_id="",
                    summary=f"MCP server {config.server_id} is disconnected",
                    error_code="disconnected",
                    risk=risk,
                )
            try:
                result = await asyncio.wait_for(
                    client.call_tool(tool_name, arguments),
                    timeout=config.timeout_seconds,
                )
            except TimeoutError:
                return tool_result_for_failure(
                    call_id="",
                    summary=f"MCP tool {tool_name} timed out after {config.timeout_seconds}s",
                    error_code="timeout",
                    risk=risk,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code = "disconnected" if self._looks_disconnected(exc) else "execution_error"
                return tool_result_for_failure(
                    call_id="",
                    summary=f"{type(exc).__name__}: {exc}",
                    error_code=code,
                    risk=risk,
                )
            return normalize_call_result(
                result,
                call_id="",
                risk=risk,
                artifacts_root=self.artifacts_root,
            )

        return handler

    def _child_env(self, config: McpServerConfig) -> dict[str, str]:
        env: dict[str, str] = {}
        allowed = set(config.env_allowlist) | {"PYTHONPATH", "REVIEW_AGENT_CONTRACT_ROOT"}
        allowed -= _SECRET_ENV_NAMES
        for name in allowed:
            if name in _SECRET_ENV_NAMES:
                continue
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        env.update(config.env)
        src_root = str(Path(__file__).resolve().parents[3])
        existing = env.get("PYTHONPATH", "")
        parts = [src_root, *([existing] if existing else []), *([self.pythonpath] if self.pythonpath else [])]
        env["PYTHONPATH"] = os.pathsep.join(part for part in parts if part)
        if self.contract_root is not None:
            env["REVIEW_AGENT_CONTRACT_ROOT"] = str(self.contract_root)
        elif "REVIEW_AGENT_CONTRACT_ROOT" not in env:
            from review_agent.mcp_servers.api_contract.catalog import catalog_root

            try:
                env["REVIEW_AGENT_CONTRACT_ROOT"] = str(catalog_root())
            except Exception:
                pass
        env.pop("DEEPSEEK_API_KEY", None)
        env.pop("GITHUB_TOKEN", None)
        return env

    @staticmethod
    def _looks_disconnected(exc: BaseException) -> bool:
        text = str(exc).lower()
        return any(token in text for token in ("disconnect", "closed", "broken pipe", "eof", "process"))
