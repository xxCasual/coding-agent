from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class AdapterError(Exception):
    def __init__(self, code: str, message: str, *, usage: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.usage = usage


class TimeoutError_(AdapterError):
    def __init__(self, message: str = "timeout") -> None:
        super().__init__("timeout", message, usage={"input_tokens": 0, "output_tokens": 0})


class InvalidResponseError(AdapterError):
    def __init__(self, message: str = "invalid_response") -> None:
        super().__init__("other", message)


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any] | None
    raw_error: str | None = None


class ChatAdapter:
    """OpenAI-compatible chat adapter with several incomplete mappings."""

    def complete(self, payload: dict[str, Any]) -> ChatResult:
        if payload.get("raw_error") == "timeout":
            raise TimeoutError_("timeout")
        if payload.get("raw_error") == "invalid_response":
            raise InvalidResponseError("invalid_response")
        if payload.get("raw_error") == "auth":
            raise AdapterError("other", "unauthorized")
        usage = payload.get("usage")
        if usage is None:
            usage = {"input_tokens": 0, "output_tokens": 0}
        return ChatResult(
            content=str(payload.get("content") or ""),
            tool_calls=list(payload.get("tool_calls") or []),
            usage=usage,
            raw_error=payload.get("raw_error"),
        )
