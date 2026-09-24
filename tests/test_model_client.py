from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from review_agent.config import Settings
from review_agent.harness.model_client import (
    ModelClient,
    OpenAICompatibleModelClient,
    messages_to_openai,
    tools_to_openai,
)
from review_agent.harness.models import (
    Message,
    ReplayCategory,
    ToolRisk,
    ToolSpec,
)


def _delta_chunk(
    *,
    content: str | None = None,
    tool_calls: list[Any] | None = None,
    finish_reason: str | None = None,
    usage: Any = None,
) -> SimpleNamespace:
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=usage)


def _tool_delta(
    index: int,
    *,
    id: str | None = None,
    name: str | None = None,
    arguments: str | None = None,
) -> SimpleNamespace:
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=id, function=function)


class FakeAsyncCompletions:
    def __init__(self, chunks: list[Any]) -> None:
        self.chunks = chunks
        self.last_kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs

        async def _gen():
            for chunk in self.chunks:
                yield chunk

        return _gen()


class FakeAsyncChat:
    def __init__(self, chunks: list[Any]) -> None:
        self.completions = FakeAsyncCompletions(chunks)


class FakeAsyncOpenAI:
    def __init__(self, chunks: list[Any]) -> None:
        self.chat = FakeAsyncChat(chunks)


def _settings(**overrides: Any) -> Settings:
    data = {
        "deepseek_api_key": "test-key",
        "deepseek_base_url": "https://example.test",
        "deepseek_model": "test-model",
        "review_agent_model_protocol": "native_tools",
    }
    data.update(overrides)
    return Settings(_env_file=None, **data)


def test_native_stream_interleaved_text_and_chunked_tool_args() -> None:
    async def _run() -> None:
        chunks = [
            _delta_chunk(content="Looking "),
            _delta_chunk(content="up."),
            _delta_chunk(
                tool_calls=[
                    _tool_delta(0, id="call_abc", name="read_file", arguments='{"pa')
                ]
            ),
            _delta_chunk(tool_calls=[_tool_delta(0, arguments='th":"a.py"}')]),
            _delta_chunk(finish_reason="tool_calls"),
            SimpleNamespace(
                choices=[],
                usage=SimpleNamespace(prompt_tokens=11, completion_tokens=3),
            ),
        ]
        client = OpenAICompatibleModelClient(
            settings=_settings(),
            client=FakeAsyncOpenAI(chunks),
        )
        messages = [Message(role="user", content="read a.py")]
        tools = [
            ToolSpec(
                name="read_file",
                description="Read a file",
                risk=ToolRisk.READ_ONLY,
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
                replay_category=ReplayCategory.REPEATABLE_READ,
            )
        ]

        events = [event async for event in client.stream(messages, tools=tools)]
        text = "".join(e.text for e in events if e.type == "text_delta")
        tool_events = [e for e in events if e.type == "tool_call_complete"]
        done = next(e for e in events if e.type == "response_done")

        assert text == "Looking up."
        assert len(tool_events) == 1
        tool_call = tool_events[0].tool_call
        assert tool_call is not None
        assert tool_call.name == "read_file"
        assert tool_call.arguments == {"path": "a.py"}
        assert tool_call.provider_call_id == "call_abc"
        assert tool_call.call_id
        assert done.usage is not None
        assert done.usage.input_tokens == 11
        assert done.usage.output_tokens == 3

        roundtrip = messages_to_openai(
            [
                Message(
                    role="assistant",
                    content=text,
                    tool_calls=[tool_call],
                ),
                Message(
                    role="tool",
                    content="ok",
                    provider_call_id=tool_call.provider_call_id,
                ),
            ]
        )
        assert roundtrip[0]["tool_calls"][0]["id"] == "call_abc"
        assert roundtrip[1]["tool_call_id"] == "call_abc"

    asyncio.run(_run())


def test_incomplete_tool_json_yields_no_executable_calls() -> None:
    async def _run() -> None:
        chunks = [
            _delta_chunk(
                tool_calls=[
                    _tool_delta(0, id="call_bad", name="read_file", arguments='{"path":')
                ]
            ),
            _delta_chunk(finish_reason="tool_calls"),
        ]
        client = OpenAICompatibleModelClient(
            settings=_settings(),
            client=FakeAsyncOpenAI(chunks),
        )
        turn = await client.complete(
            [Message(role="user", content="x")],
            tools=[
                ToolSpec(
                    name="read_file",
                    description="Read",
                    risk=ToolRisk.READ_ONLY,
                    input_schema={"type": "object", "properties": {}},
                )
            ],
        )
        assert turn.tool_calls == []
        assert turn.raw_error
        assert "invalid" in turn.raw_error.lower() or "Incomplete" in turn.raw_error

    asyncio.run(_run())


def test_missing_usage_stays_none_not_zero() -> None:
    async def _run() -> None:
        chunks = [
            _delta_chunk(content="hello"),
            _delta_chunk(finish_reason="stop"),
        ]
        client = OpenAICompatibleModelClient(
            settings=_settings(),
            client=FakeAsyncOpenAI(chunks),
        )
        turn = await client.complete([Message(role="user", content="hi")])
        assert turn.usage is not None
        assert turn.usage.input_tokens is None
        assert turn.usage.output_tokens is None
        assert turn.usage.estimated_cost is None
        assert turn.content == "hello"

    asyncio.run(_run())


def test_legacy_and_native_normalize_to_model_turn_result() -> None:
    async def _run() -> None:
        native_chunks = [
            _delta_chunk(
                tool_calls=[
                    _tool_delta(
                        0,
                        id="call_1",
                        name="git_status",
                        arguments="{}",
                    )
                ]
            ),
            _delta_chunk(finish_reason="tool_calls"),
        ]
        legacy_chunks = [
            _delta_chunk(content='{"type":"tool","tool":"git_status","input":{}}'),
            _delta_chunk(finish_reason="stop"),
        ]
        tools = [
            ToolSpec(
                name="git_status",
                description="status",
                risk=ToolRisk.READ_ONLY,
                input_schema={"type": "object", "properties": {}},
            )
        ]

        native = OpenAICompatibleModelClient(
            settings=_settings(review_agent_model_protocol="native_tools"),
            client=FakeAsyncOpenAI(native_chunks),
        )
        legacy = OpenAICompatibleModelClient(
            settings=_settings(review_agent_model_protocol="legacy_json"),
            client=FakeAsyncOpenAI(legacy_chunks),
        )
        native_turn = await native.complete(
            [Message(role="user", content="s")],
            tools=tools,
            protocol_mode="native_tools",
        )
        legacy_turn = await legacy.complete(
            [Message(role="user", content="s")],
            protocol_mode="legacy_json",
        )
        assert len(native_turn.tool_calls) == 1
        assert len(legacy_turn.tool_calls) == 1
        assert native_turn.tool_calls[0].name == "git_status"
        assert legacy_turn.tool_calls[0].name == "git_status"
        assert native_turn.tool_calls[0].provider_call_id == "call_1"
        assert legacy_turn.tool_calls[0].provider_call_id is None

    asyncio.run(_run())


def test_unsupported_schema_is_rejected() -> None:
    with pytest.raises(Exception) as excinfo:
        tools_to_openai(
            [
                ToolSpec(
                    name="bad",
                    description="bad",
                    risk=ToolRisk.READ_ONLY,
                    input_schema={"type": "array"},
                )
            ]
        )
    assert excinfo.value.error_code == "unsupported_schema"


def test_messages_to_openai_requires_provider_call_id_for_tool_role() -> None:
    with pytest.raises(Exception) as excinfo:
        messages_to_openai([Message(role="tool", content="x")])
    assert excinfo.value.error_code == "missing_provider_call_id"


class _RaisingAsyncCompletions:
    async def create(self, **kwargs: Any) -> Any:
        del kwargs
        err = RuntimeError("Connection error.")
        err.__cause__ = OSError("connect to 127.0.0.1 port 20396 failed: Connection refused")
        raise err


class _RaisingAsyncOpenAI:
    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=_RaisingAsyncCompletions())


def test_stream_connection_error_includes_cause() -> None:
    async def _run() -> None:
        client = OpenAICompatibleModelClient(
            settings=_settings(),
            client=_RaisingAsyncOpenAI(),
        )
        turn = await client.complete([Message(role="user", content="hi")])
        assert turn.error_code == "stream_failed"
        assert turn.raw_error
        assert "Connection error" in turn.raw_error
        assert "20396" in turn.raw_error or "Connection refused" in turn.raw_error

    asyncio.run(_run())


def test_coding_model_client_protocol_is_async_complete() -> None:
    assert "complete" in ModelClient.__dict__
    assert "stream" in ModelClient.__dict__
    assert "complete_json" not in ModelClient.__dict__


def _budget_settings(tmp_path, **overrides):
    import json
    ledger = tmp_path / "budget.json"
    if not ledger.exists():
        ledger.write_text(json.dumps({"limit_cny": 5, "peak_cny_per_million": {"input": 2, "output": 8},
            "requests": [{"id": "rag-history", "accounted_cny": 1.5, "state": "reserved_or_unknown"}]}))
    return _settings(deepseek_model="deepseek-flash", deepseek_base_url="https://api.deepseek.com",
        review_agent_budget_path=str(ledger), review_agent_price_input_per_1m=2,
        review_agent_price_output_per_1m=8, review_agent_price_as_of="2026-09-22", **overrides)


@pytest.mark.parametrize("protocol", ["native_tools", "legacy_json"])
def test_budget_uses_real_sdk_limits_and_preserves_history(tmp_path, protocol):
    import json
    import httpx
    from openai import AsyncOpenAI

    settings = _budget_settings(tmp_path, review_agent_model_protocol=protocol)
    sent = []
    def handle(request):
        payload = json.loads(request.content)
        sent.append(payload)
        ledger = json.loads((tmp_path / "budget.json").read_text())
        assert ledger["requests"][-1]["accounted_cny"] == 0.5  # Persisted before transport.
        body = {"model": "deepseek-flash", "choices": [{"index": 0, "delta": {"content": '{"type":"final","message":"done"}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=f"data: {json.dumps(body)}\n\ndata: [DONE]\n\n")
    async def run():
        sdk = AsyncOpenAI(api_key="test", base_url=settings.deepseek_base_url, max_retries=3,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
        client = OpenAICompatibleModelClient(settings, client=sdk)
        turn = await client.complete([Message(role="user", content="inspect")])
        await client.aclose()
        assert not turn.raw_error and turn.usage.input_tokens == 100
    asyncio.run(run())
    assert len(sent) == 1
    assert sent[0]["max_tokens"] == 4096 and sent[0]["thinking"] == {"type": "disabled"}
    records = json.loads((tmp_path / "budget.json").read_text())["requests"]
    assert records[0] == {"id": "rag-history", "accounted_cny": 1.5, "state": "reserved_or_unknown"}
    assert records[1]["accounted_cny"] == pytest.approx(0.00036)


def test_budget_transport_failure_no_hidden_retry_and_restart_cannot_reset(tmp_path):
    import json
    import httpx
    from openai import AsyncOpenAI

    settings = _budget_settings(tmp_path)
    sent = []
    def handle(request):
        sent.append(request)
        return httpx.Response(503, json={"error": {"message": "temporary"}})
    async def run():
        for _ in range(8):
            # Reconstruct the client just as separate workers/resumed runs would.
            sdk = AsyncOpenAI(api_key="test", base_url=settings.deepseek_base_url, max_retries=3,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
            client = OpenAICompatibleModelClient(settings, client=sdk)
            turn = await client.complete([Message(role="user", content="inspect")])
            await client.aclose()
            assert turn.raw_error
        assert turn.error_code == "budget_exhausted"
    asyncio.run(run())
    assert len(sent) == 7  # History 1.5 + 7 unknown requests at 0.5 = 5 CNY.
    records = json.loads((tmp_path / "budget.json").read_text())["requests"]
    assert sum(item["accounted_cny"] for item in records) == 5
    assert all(item["state"] == "reserved_or_unknown" for item in records)


def test_budget_rejects_oversize_invalid_ledger_and_unpriced_model(tmp_path):
    import json
    from dataclasses import replace
    from review_agent.harness.budget import BudgetLedger, BudgetError

    settings = _budget_settings(tmp_path)
    guard = BudgetLedger(settings.review_agent_budget_path)
    profile = settings.get_model_profile()
    for bad_profile, request in [(profile, {"messages": "x" * 100001}),
                                 (replace(profile, model="deepseek-v4-pro"), {}),
                                 (replace(profile, price_input_per_1m=None), {})]:
        with pytest.raises(BudgetError):
            guard.reserve(bad_profile, request)
    assert len(json.loads((tmp_path / "budget.json").read_text())["requests"]) == 1
    for bad in ['{}', '{broken', '{"limit_cny":5,"requests":[]}']:
        (tmp_path / "budget.json").write_text(bad)
        with pytest.raises(BudgetError):
            guard.reserve(profile, {})


def test_budget_concurrent_reservations_do_not_overspend(tmp_path):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from review_agent.harness.budget import BudgetLedger, BudgetError

    settings = _budget_settings(tmp_path)
    def reserve(_):
        try:
            return BudgetLedger(settings.review_agent_budget_path).reserve(settings.get_model_profile(), {})
        except BudgetError:
            return None
    with ThreadPoolExecutor(max_workers=12) as pool:
        ids = list(pool.map(reserve, range(12)))
    assert len([item for item in ids if item]) == 7
    data = json.loads((tmp_path / "budget.json").read_text())
    assert sum(item["accounted_cny"] for item in data["requests"]) == 5


def test_budget_cancelled_stream_keeps_reservation(tmp_path):
    import json
    import httpx
    from openai import AsyncOpenAI
    settings = _budget_settings(tmp_path)
    started = asyncio.Event()
    class HangingBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"index":0,"delta":{"content":"partial"}}]}\n\n'
            started.set()
            await asyncio.Event().wait()
    async def run():
        sdk = AsyncOpenAI(api_key="test", base_url=settings.deepseek_base_url,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200,
                headers={"content-type": "text/event-stream"}, stream=HangingBody()))))
        client = OpenAICompatibleModelClient(settings, client=sdk)
        task = asyncio.create_task(client.complete([Message(role="user", content="inspect")]))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.aclose()
    asyncio.run(run())
    item = json.loads((tmp_path / "budget.json").read_text())["requests"][-1]
    assert item["accounted_cny"] == 0.5 and item["state"] == "reserved_or_unknown"


def test_budget_increased_cap_still_enforces_ten_cny_ceiling(tmp_path):
    import json
    from review_agent.harness.budget import BudgetLedger, BudgetError

    settings = _budget_settings(tmp_path)
    path = tmp_path / "budget.json"
    data = json.loads(path.read_text())
    data["limit_cny"] = 100  # A ledger alone cannot lift the code's authorized ceiling.
    data["requests"][0]["accounted_cny"] = 9.5
    path.write_text(json.dumps(data))
    guard = BudgetLedger(str(path))
    request_id = guard.reserve(settings.get_model_profile(), {})
    with pytest.raises(BudgetError, match="exhausted"):
        guard.reserve(settings.get_model_profile(), {})
    stored = json.loads(path.read_text())
    assert stored["requests"][0] == data["requests"][0]
    assert stored["requests"][-1]["id"] == request_id
    assert sum(item["accounted_cny"] for item in stored["requests"]) == 10
