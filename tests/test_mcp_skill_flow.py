from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

from review_agent.config import Settings
from review_agent.harness.approval import ApprovalPolicy
from review_agent.harness.memory import AgentMemory
from review_agent.harness.models import (
    ModelTurnResult,
    RegisteredTool,
    ReplayCategory,
    ToolCall,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from review_agent.harness.runtime import AgentRuntime
from review_agent.harness.task_store import AcceptanceSpec, InMemoryTaskStore
from review_agent.mcp_servers.api_contract.logic import (
    get_endpoint_contract,
    validate_response_sample,
)
from tests.test_coding_runtime import FakeAsyncModelClient

REPO = Path(__file__).resolve().parents[1]
CONTRACTS = REPO / "examples" / "contracts"
APP_SOURCE = REPO / "examples" / "fastapi-items" / "app.py"

GET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contract_id", "method", "path"],
    "properties": {
        "contract_id": {"type": "string"},
        "method": {"type": "string"},
        "path": {"type": "string"},
    },
}
VALIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contract_id", "method", "path", "status_code", "sample"],
    "properties": {
        "contract_id": {"type": "string"},
        "method": {"type": "string"},
        "path": {"type": "string"},
        "status_code": {"type": "integer"},
        "sample": {"type": "object"},
    },
}


def _tool(name: str, arguments: dict) -> ToolCall:
    call_id = str(uuid.uuid4())
    return ToolCall(name=name, arguments=arguments, call_id=call_id, provider_call_id=call_id)


def _json_handler(fn):
    def handler(arguments: dict) -> ToolResult:
        payload = fn(**arguments)
        body = json.dumps(payload, ensure_ascii=False)
        return ToolResult(success=True, summary=body[:200], stdout=body, risk_level=ToolRisk.READ_ONLY)

    return handler


def _register_contract_tools(registry) -> None:
    specs = [
        ("mcp__api_contract__get_endpoint_contract", GET_SCHEMA, get_endpoint_contract),
        ("mcp__api_contract__validate_response_sample", VALIDATE_SCHEMA, validate_response_sample),
    ]
    for name, schema, fn in specs:
        registry.register(
            RegisteredTool(
                spec=ToolSpec(
                    name=name,
                    description=name,
                    risk=ToolRisk.READ_ONLY,
                    input_schema=schema,
                    replay_category=ReplayCategory.REPEATABLE_READ,
                ),
                handler=_json_handler(fn),
                params_model=None,
            )
        )


def test_implement_fastapi_skill_get_patch_validate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REVIEW_AGENT_CONTRACT_ROOT", str(CONTRACTS))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    (workspace / "app.py").write_text(APP_SOURCE.read_text(encoding="utf-8"), encoding="utf-8")
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "add", "app.py"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-m", "init"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -12,10 +12,9 @@ class Item(BaseModel):
     id: str
     name: str
-    # Intentional contract bug: response quantity must be integer.
-    quantity: str
+    quantity: int
 
 
 @app.post("/items", response_model=Item, status_code=201)
 def create_item(payload: ItemCreate) -> Item:
-    return Item(id="item-1", name=payload.name, quantity=str(payload.quantity))
+    return Item(id="item-1", name=payload.name, quantity=payload.quantity)
"""
    settings = Settings(
        review_agent_data_root=str(tmp_path / "ra-data"),
        review_agent_approval_mode="auto",
        review_agent_executor_backend="host",
        review_agent_memory_dir=".memory",
        review_agent_agent_max_steps=24,
        review_agent_max_verification_repairs=2,
        review_agent_command_timeout_seconds=30,
    )
    model = FakeAsyncModelClient(
        [
            ModelTurnResult(
                tool_calls=[_tool("load_skill", {"name": "implement-fastapi-endpoint"})],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(
                tool_calls=[
                    _tool(
                        "mcp__api_contract__get_endpoint_contract",
                        {"contract_id": "items-v1", "method": "POST", "path": "/items"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(
                tool_calls=[_tool("apply_patch", {"patch": patch})],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(
                tool_calls=[
                    _tool(
                        "mcp__api_contract__validate_response_sample",
                        {
                            "contract_id": "items-v1",
                            "method": "POST",
                            "path": "/items",
                            "status_code": 201,
                            "sample": {"id": "item-1", "name": "widget", "quantity": 2},
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ModelTurnResult(content="fixed quantity type", finish_reason="stop"),
        ]
    )
    runtime = AgentRuntime(
        workspace,
        model_client=model,
        approval_policy=ApprovalPolicy("auto"),
        memory=AgentMemory(workspace, settings=settings),
        settings=settings,
        task_store=InMemoryTaskStore(),
        isolate_workspace=False,
    )
    _register_contract_tools(runtime.tool_registry)
    session_id = runtime.create_session()
    run_id = runtime.start_run(
        session_id,
        "fix POST /items response quantity type using contract items-v1",
        acceptance=AcceptanceSpec(mode="not_applicable"),
    )
    import asyncio

    final = asyncio.run(runtime.execute_run(run_id, approver=lambda _r: True))
    names = [event.tool_call.name for event in final.events if event.tool_call]
    assert "load_skill" in names
    assert "mcp__api_contract__get_endpoint_contract" in names
    assert "apply_patch" in names
    assert "mcp__api_contract__validate_response_sample" in names
    assert names.index("mcp__api_contract__get_endpoint_contract") < names.index("apply_patch")
    assert names.index("apply_patch") < names.index("mcp__api_contract__validate_response_sample")
    validate = next(
        event
        for event in final.events
        if event.tool_call
        and event.tool_call.name == "mcp__api_contract__validate_response_sample"
        and event.type == "tool.finished"
    )
    assert validate.tool_result is not None
    assert validate.tool_result.success
    assert '"status": "valid"' in (validate.tool_result.stdout or "")
    assert "quantity: int" in (workspace / "app.py").read_text(encoding="utf-8")
