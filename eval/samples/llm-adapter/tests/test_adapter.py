from adapter import AdapterError, ChatAdapter, TimeoutError_


def test_success_keeps_usage():
    result = ChatAdapter().complete(
        {"content": "ok", "usage": {"input_tokens": 10, "output_tokens": 2}}
    )
    assert result.content == "ok"
    assert result.usage == {"input_tokens": 10, "output_tokens": 2}


def test_timeout_does_not_invent_zero_usage():
    try:
        ChatAdapter().complete({"raw_error": "timeout", "usage": None})
    except TimeoutError_ as exc:
        assert exc.usage is None
        return
    raise AssertionError("expected timeout error")
