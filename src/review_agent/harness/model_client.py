from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Any, Protocol

from openai import AsyncOpenAI, OpenAI

from review_agent.config import ModelProfile, Settings, get_settings
from review_agent.harness.budget import BudgetError, BudgetLedger
from review_agent.harness.models import (
    Message,
    ModelStreamEvent,
    ModelTurnResult,
    ToolCall,
    ToolSpec,
    Usage,
)


class ModelClient(Protocol):
    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        profile_id: str | None = None,
        protocol_mode: str | None = None,
    ) -> ModelTurnResult:
        ...

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        profile_id: str | None = None,
        protocol_mode: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        ...


class ModelProtocolError(Exception):
    def __init__(self, message: str, *, error_code: str = "model_protocol_error") -> None:
        super().__init__(message)
        self.error_code = error_code


def _exception_text(exc: BaseException) -> str:
    parts = [str(exc).strip() or type(exc).__name__]
    seen = {id(exc)}
    cause: BaseException | None = exc.__cause__ or exc.__context__
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        text = str(cause).strip() or type(cause).__name__
        if text not in parts[-1]:
            parts.append(text)
        cause = cause.__cause__ or cause.__context__
    return " | ".join(parts)


def messages_to_openai(messages: Iterable[Message]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for message in messages:
        item: dict[str, Any] = {"role": message.role}
        if message.role == "assistant" and message.tool_calls:
            item["content"] = message.content or None
            item["tool_calls"] = [
                {
                    "id": tc.provider_call_id or tc.call_id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in message.tool_calls
            ]
        elif message.role == "tool":
            if not message.provider_call_id:
                raise ModelProtocolError(
                    "tool messages require provider_call_id",
                    error_code="missing_provider_call_id",
                )
            item["tool_call_id"] = message.provider_call_id
            item["content"] = message.content
        else:
            item["content"] = message.content
        payload.append(item)
    return payload


def tools_to_openai(tools: list[ToolSpec] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for spec in tools:
        schema = spec.input_schema
        _validate_tool_schema(spec.name, schema)
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": schema,
                },
            }
        )
    return converted


def _validate_tool_schema(name: str, schema: dict[str, Any]) -> None:
    if not isinstance(schema, dict):
        raise ModelProtocolError(
            f"Unsupported tool schema for {name}: not an object",
            error_code="unsupported_schema",
        )
    schema_type = schema.get("type")
    if schema_type != "object":
        raise ModelProtocolError(
            f"Unsupported tool schema for {name}: type must be 'object' (got {schema_type!r})",
            error_code="unsupported_schema",
        )
    try:
        json.dumps(schema)
    except (TypeError, ValueError) as exc:
        raise ModelProtocolError(
            f"Unsupported tool schema for {name}: not JSON-serializable ({exc})",
            error_code="unsupported_schema",
        ) from exc


def _estimate_cost(
    profile: ModelProfile,
    input_tokens: int | None,
    output_tokens: int | None,
) -> tuple[float | None, str | None]:
    if (
        profile.price_input_per_1m is None
        or profile.price_output_per_1m is None
        or input_tokens is None
        or output_tokens is None
    ):
        return None, None
    cost = (
        input_tokens / 1_000_000 * profile.price_input_per_1m
        + output_tokens / 1_000_000 * profile.price_output_per_1m
    )
    return cost, profile.id


def _usage_from_response(
    profile: ModelProfile,
    raw_usage: Any,
    *,
    latency_ms: float,
) -> Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    if raw_usage is not None:
        prompt = getattr(raw_usage, "prompt_tokens", None)
        completion = getattr(raw_usage, "completion_tokens", None)
        if prompt is None and isinstance(raw_usage, dict):
            prompt = raw_usage.get("prompt_tokens")
            completion = raw_usage.get("completion_tokens")
        if prompt is not None:
            input_tokens = int(prompt)
        if completion is not None:
            output_tokens = int(completion)
    estimated, price_config_id = _estimate_cost(profile, input_tokens, output_tokens)
    return Usage(
        profile_id=profile.id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model_latency_ms=latency_ms,
        estimated_cost=estimated,
        price_config_id=price_config_id,
        price_as_of=profile.price_as_of if estimated is not None else None,
    )


def _parse_legacy_action(content: str, *, call_id: str) -> ModelTurnResult:
    try:
        data = json.loads(content or "{}")
    except json.JSONDecodeError as exc:
        return ModelTurnResult(
            raw_error=f"Invalid legacy JSON: {exc}",
            finish_reason="error",
        )
    if not isinstance(data, dict):
        return ModelTurnResult(
            raw_error="Legacy JSON root must be an object",
            finish_reason="error",
        )
    action_type = data.get("type")
    if action_type == "final":
        return ModelTurnResult(
            content=str(data.get("message") or ""),
            finish_reason="stop",
        )
    if action_type == "tool":
        name = data.get("tool") or data.get("name")
        if not isinstance(name, str) or not name:
            return ModelTurnResult(
                raw_error="Legacy tool action missing tool name",
                finish_reason="error",
            )
        arguments = data.get("input")
        if arguments is None:
            arguments = data.get("arguments", {})
        if not isinstance(arguments, dict):
            return ModelTurnResult(
                raw_error="Legacy tool arguments must be an object",
                finish_reason="error",
            )
        return ModelTurnResult(
            tool_calls=[
                ToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=call_id,
                    provider_call_id=None,
                )
            ],
            finish_reason="tool_calls",
        )
    return ModelTurnResult(
        raw_error=f"Unknown legacy action type: {action_type!r}",
        finish_reason="error",
    )


class OpenAICompatibleModelClient:
    """Async OpenAI-compatible client with native tools or legacy JSON mode."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
        sync_client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._sync_client = sync_client
        self._budget = BudgetLedger(self.settings.review_agent_budget_path) if self.settings.review_agent_budget_path else None

    def resolve_profile(self, profile_id: str | None = None) -> ModelProfile:
        return self.settings.get_model_profile(profile_id)

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        profile_id: str | None = None,
        protocol_mode: str | None = None,
    ) -> AsyncIterator[ModelStreamEvent]:
        profile = self.resolve_profile(profile_id)
        mode = protocol_mode or profile.protocol_mode
        if mode == "legacy_json":
            async for event in self._stream_legacy(messages, profile=profile):
                yield event
            return
        if mode != "native_tools":
            yield ModelStreamEvent(
                type="error",
                error=f"Unknown protocol_mode: {mode}",
                error_code="unknown_protocol_mode",
            )
            return
        async for event in self._stream_native(messages, tools, profile=profile):
            yield event

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        *,
        profile_id: str | None = None,
        protocol_mode: str | None = None,
    ) -> ModelTurnResult:
        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        usage: Usage | None = None
        finish_reason: str | None = None
        raw_error: str | None = None
        error_code: str | None = None
        async for event in self.stream(
            messages,
            tools,
            profile_id=profile_id,
            protocol_mode=protocol_mode,
        ):
            if event.type == "text_delta":
                content_parts.append(event.text)
            elif event.type == "tool_call_complete" and event.tool_call is not None:
                tool_calls.append(event.tool_call)
            elif event.type == "response_done":
                usage = event.usage
                finish_reason = event.finish_reason
            elif event.type == "error":
                raw_error = event.error
                error_code = event.error_code
                finish_reason = "error"
        return ModelTurnResult(
            content="".join(content_parts),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=finish_reason,
            raw_error=raw_error,
            error_code=error_code,
        )

    async def _stream_native(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None,
        *,
        profile: ModelProfile,
    ) -> AsyncIterator[ModelStreamEvent]:
        try:
            openai_tools = tools_to_openai(tools)
            openai_messages = messages_to_openai(messages)
        except ModelProtocolError as exc:
            yield ModelStreamEvent(type="error", error=str(exc), error_code=exc.error_code)
            return

        api_key = self.settings.api_key_for_profile(profile)
        if not api_key and self._client is None:
            yield ModelStreamEvent(
                type="error",
                error=f"{profile.api_key_env} is not configured",
                error_code="missing_api_key",
            )
            return

        started = time.perf_counter()
        pending: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None
        raw_usage: Any = None
        try:
            stream = self._provider_stream(profile,
                model=profile.model,
                messages=openai_messages,
                tools=openai_tools,
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    raw_usage = chunk.usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                text = getattr(delta, "content", None)
                if text:
                    yield ModelStreamEvent(type="text_delta", text=text)
                tool_deltas = getattr(delta, "tool_calls", None) or []
                for tool_delta in tool_deltas:
                    index = int(getattr(tool_delta, "index", 0) or 0)
                    bucket = pending.setdefault(
                        index, {"id": "", "name": "", "arguments": ""}
                    )
                    provider_id = getattr(tool_delta, "id", None)
                    if provider_id:
                        bucket["id"] = provider_id
                    function = getattr(tool_delta, "function", None)
                    if function is not None:
                        name = getattr(function, "name", None)
                        if name:
                            bucket["name"] = name
                        arguments = getattr(function, "arguments", None)
                        if arguments:
                            bucket["arguments"] += arguments
        except Exception as exc:  # noqa: BLE001 - surface provider failures as stream errors
            yield ModelStreamEvent(
                type="error",
                error=f"Model stream failed: {_exception_text(exc)}",
                error_code="budget_exhausted" if isinstance(exc, BudgetError) else "stream_failed",
            )
            return

        latency_ms = (time.perf_counter() - started) * 1000
        executable: list[ToolCall] = []
        for index in sorted(pending):
            bucket = pending[index]
            raw_args = bucket["arguments"]
            try:
                parsed = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                yield ModelStreamEvent(
                    type="error",
                    error=f"Incomplete or invalid tool arguments JSON at index {index}",
                    error_code="invalid_tool_arguments",
                )
                usage = _usage_from_response(profile, raw_usage, latency_ms=latency_ms)
                yield ModelStreamEvent(
                    type="response_done",
                    usage=usage,
                    finish_reason="error",
                )
                return
            if not isinstance(parsed, dict):
                yield ModelStreamEvent(
                    type="error",
                    error=f"Tool arguments must be a JSON object at index {index}",
                    error_code="invalid_tool_arguments",
                )
                usage = _usage_from_response(profile, raw_usage, latency_ms=latency_ms)
                yield ModelStreamEvent(
                    type="response_done",
                    usage=usage,
                    finish_reason="error",
                )
                return
            if not bucket["name"]:
                yield ModelStreamEvent(
                    type="error",
                    error=f"Tool call missing name at index {index}",
                    error_code="invalid_tool_call",
                )
                usage = _usage_from_response(profile, raw_usage, latency_ms=latency_ms)
                yield ModelStreamEvent(
                    type="response_done",
                    usage=usage,
                    finish_reason="error",
                )
                return
            tool_call = ToolCall(
                name=bucket["name"],
                arguments=parsed,
                call_id=str(uuid.uuid4()),
                provider_call_id=bucket["id"] or None,
            )
            executable.append(tool_call)
            yield ModelStreamEvent(type="tool_call_complete", tool_call=tool_call)

        usage = _usage_from_response(profile, raw_usage, latency_ms=latency_ms)
        yield ModelStreamEvent(
            type="response_done",
            usage=usage,
            finish_reason=finish_reason or ("tool_calls" if executable else "stop"),
        )

    async def _stream_legacy(
        self,
        messages: list[Message],
        *,
        profile: ModelProfile,
    ) -> AsyncIterator[ModelStreamEvent]:
        openai_messages = messages_to_openai(messages)
        api_key = self.settings.api_key_for_profile(profile)
        if not api_key and self._client is None:
            yield ModelStreamEvent(
                type="error",
                error=f"{profile.api_key_env} is not configured",
                error_code="missing_api_key",
            )
            return

        started = time.perf_counter()
        content_parts: list[str] = []
        raw_usage: Any = None
        finish_reason: str | None = None
        try:
            stream = self._provider_stream(profile,
                model=profile.model,
                messages=openai_messages,
                response_format={"type": "json_object"},
                stream=True,
                stream_options={"include_usage": True},
            )
            async for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    raw_usage = chunk.usage
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = getattr(choice, "delta", None)
                text = getattr(delta, "content", None) if delta is not None else None
                if text:
                    content_parts.append(text)
                    yield ModelStreamEvent(type="text_delta", text=text)
        except Exception as exc:  # noqa: BLE001
            yield ModelStreamEvent(
                type="error",
                error=f"Model stream failed: {_exception_text(exc)}",
                error_code="budget_exhausted" if isinstance(exc, BudgetError) else "stream_failed",
            )
            return

        latency_ms = (time.perf_counter() - started) * 1000
        turn = _parse_legacy_action("".join(content_parts), call_id=str(uuid.uuid4()))
        if turn.raw_error:
            yield ModelStreamEvent(
                type="error",
                error=turn.raw_error,
                error_code="invalid_legacy_action",
            )
        for tool_call in turn.tool_calls:
            yield ModelStreamEvent(type="tool_call_complete", tool_call=tool_call)
        usage = _usage_from_response(profile, raw_usage, latency_ms=latency_ms)
        yield ModelStreamEvent(
            type="response_done",
            usage=usage,
            finish_reason=turn.finish_reason or finish_reason,
        )

    async def _provider_stream(self, profile: ModelProfile, **request):
        client = self._async_client_or_create(profile)
        reservation = None
        raw_usage = None
        response_model = None
        if self._budget:
            # Avoid unaccounted HTTP retries and unsupported thinking/tool history.
            client = client.with_options(max_retries=0)
            request.update(max_tokens=self._budget.MAX_OUTPUT, extra_body={"thinking": {"type": "disabled"}})
            reservation = self._budget.reserve(profile, request)
        stream = await client.chat.completions.create(**request)
        try:
            async for chunk in stream:
                raw_usage = getattr(chunk, "usage", None) or raw_usage
                response_model = getattr(chunk, "model", None) or response_model
                yield chunk
            if self._budget and reservation:
                self._budget.settle(reservation, raw_usage, response_model)
        finally:
            # Cancellation/transport failure leaves the reservation charged as unknown.
            if hasattr(stream, "close"):
                await stream.close()

    async def aclose(self) -> None:
        if self._client is not None and hasattr(self._client, "close"):
            await self._client.close()
            self._client = None

    def _async_client_or_create(self, profile: ModelProfile) -> Any:
        if self._client is not None:
            return self._client
        api_key = self.settings.api_key_for_profile(profile) or ""
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=profile.base_url,
            timeout=profile.timeout_seconds,
            max_retries=0 if self._budget else profile.max_retries,
        )
        return self._client

    def _sync_client_or_create(self, profile: ModelProfile) -> Any:
        if self._sync_client is not None:
            return self._sync_client
        api_key = self.settings.api_key_for_profile(profile) or ""
        self._sync_client = OpenAI(
            api_key=api_key,
            base_url=profile.base_url,
            timeout=profile.timeout_seconds,
            max_retries=0 if self._budget else profile.max_retries,
        )
        return self._sync_client


class DeepSeekModelClient:
    """Legacy sync JSON-mode client for AgentRuntime / CLI until M03 rewires."""

    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client
        self._compat = OpenAICompatibleModelClient(settings=self.settings, sync_client=client)

    def complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        if self._compat._budget:
            raise BudgetError("Budgeted runs must use the coding model client, not the legacy PR client.")
        if not self.settings.deepseek_api_key and self._client is None:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        profile = self.settings.get_model_profile("deepseek")
        response = self._compat._sync_client_or_create(profile).chat.completions.create(
            model=profile.model,
            messages=messages,
            response_format={"type": "json_object"},
            stream=False,
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)
