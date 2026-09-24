"""Stdio MCP checks for the review-agent interpreter (pytest may be missing).

Uses the official v2 Client only so this file does not import pydantic_settings.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

os.environ.setdefault("REVIEW_AGENT_CONTRACT_ROOT", str(REPO / "examples" / "contracts"))
os.environ.setdefault("PYTHONPATH", str(SRC))


def _server_env() -> dict[str, str]:
    return {
        "PYTHONPATH": str(SRC),
        "REVIEW_AGENT_CONTRACT_ROOT": str(REPO / "examples" / "contracts"),
    }


async def check_stdio_roundtrip() -> None:
    from mcp import Client, StdioServerParameters

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "review_agent.mcp_servers.api_contract"],
        env=_server_env(),
    )
    async with Client(params, read_timeout_seconds=20) as client:
        listed = await client.list_tools()
        names = [tool.name for tool in listed.tools]
        assert "get_endpoint_contract" in names, names
        assert "compare_contract_versions" in names, names
        assert "validate_response_sample" in names, names
        result = await client.call_tool(
            "get_endpoint_contract",
            {"contract_id": "items-v1", "method": "POST", "path": "/items"},
        )
        assert not result.is_error, result
        body = result.structured_content or {}
        assert body.get("contract_id") == "items-v1"
        assert body.get("contract_hash")
        text = ""
        for block in result.content or []:
            text += getattr(block, "text", "") or ""
        assert "items-v1" in text or body.get("source") == "items-v1"


async def check_timeout_cleans_process(tmp: Path) -> None:
    from mcp import Client, StdioServerParameters

    hang = tmp / "hang.py"
    pidfile = tmp / "hang.pid"
    hang.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(hang), str(pidfile)],
        env=_server_env(),
    )
    client = Client(params, read_timeout_seconds=1)
    raised = False
    try:
        await client.__aenter__()
    except Exception:
        raised = True
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            pass
    else:
        await client.__aexit__(None, None, None)
    assert raised, "hanging stdio process should fail initialize/timeout"
    deadline = time.time() + 5
    pid = None
    while time.time() < deadline:
        if pidfile.exists() and pidfile.read_text(encoding="utf-8").strip():
            pid = int(pidfile.read_text(encoding="utf-8").strip())
            break
        await asyncio.sleep(0.05)
    if pid is None:
        return
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"hang process {pid} still alive after Client close")


def main() -> int:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="m06-mcp-"))
    print("tmp", tmp)
    asyncio.run(check_stdio_roundtrip())
    print("stdio roundtrip ok")
    asyncio.run(check_timeout_cleans_process(tmp))
    print("timeout cleanup ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
