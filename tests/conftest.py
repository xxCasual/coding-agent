from __future__ import annotations

import os

import pytest

from review_agent.config import get_settings


@pytest.fixture(autouse=True)
def _force_host_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests use explicit host-dev mode; docker tests construct Executor(backend='docker')."""
    monkeypatch.setenv("REVIEW_AGENT_EXECUTOR_BACKEND", "host")
    os.environ["REVIEW_AGENT_EXECUTOR_BACKEND"] = "host"
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
