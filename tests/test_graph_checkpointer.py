from __future__ import annotations

import asyncio
import os
from typing import TypedDict

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

from sqlalchemy.exc import OperationalError

from review_agent.db.engine import create_db_engine
from review_agent.services.graph_checkpointer import open_coding_checkpointer

DATABASE_URL = os.environ.get("REVIEW_AGENT_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL") or ""


def _connect_or_skip() -> str:
    if not DATABASE_URL:
        pytest.skip("PostgreSQL not configured (REVIEW_AGENT_DATABASE_URL)")
    try:
        engine = create_db_engine(DATABASE_URL)
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
    except OperationalError as exc:
        pytest.skip(f"PostgreSQL unavailable: {exc}")
    return DATABASE_URL


class _CounterState(TypedDict):
    n: int


async def _inc(state: _CounterState) -> dict[str, int]:
    return {"n": int(state.get("n") or 0) + 1}


async def _roundtrip(url: str) -> int:
    from langgraph.graph import END, START, StateGraph

    thread = {"configurable": {"thread_id": "run-checkpoint-d01"}}
    async with open_coding_checkpointer(url) as saver:
        graph = StateGraph(_CounterState)
        graph.add_node("inc", _inc)
        graph.add_edge(START, "inc")
        graph.add_edge("inc", END)
        app = graph.compile(checkpointer=saver)
        await app.ainvoke({"n": 0}, config=thread)
        snapshot = await saver.aget_tuple(thread)
        assert snapshot is not None
        values = snapshot.checkpoint.get("channel_values") or {}
        return int(values.get("n") or 0)


def test_postgres_checkpointer_setup_and_reload() -> None:
    url = _connect_or_skip()
    assert asyncio.run(_roundtrip(url)) == 1
