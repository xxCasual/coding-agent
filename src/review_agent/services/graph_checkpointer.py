from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver


def sqlalchemy_url_to_psycopg(database_url: str) -> str:
    """Convert SQLAlchemy URLs to a psycopg connection string for the official saver."""
    url = (database_url or "").strip()
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url.split("://", 1)[1]
    if url.startswith("postgresql+psycopg2://"):
        return "postgresql://" + url.split("://", 1)[1]
    return url


@asynccontextmanager
async def open_coding_checkpointer(database_url: str = "") -> AsyncIterator[Any]:
    """Yield InMemorySaver, or AsyncPostgresSaver after official setup().

    Checkpoint tables are independent of Alembic business migrations.
    """
    url = (database_url or "").strip()
    if not url:
        yield InMemorySaver()
        return
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(sqlalchemy_url_to_psycopg(url)) as saver:
        await saver.setup()
        yield saver


async def setup_postgres_checkpointer(database_url: str) -> None:
    async with open_coding_checkpointer(database_url):
        return
