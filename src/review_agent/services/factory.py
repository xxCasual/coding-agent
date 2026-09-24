from __future__ import annotations

from functools import lru_cache

from review_agent.config import get_settings
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager


@lru_cache(maxsize=1)
def get_workspace_manager() -> WorkspaceManager:
    return WorkspaceManager(get_settings().review_agent_data_root)


@lru_cache(maxsize=1)
def build_task_service() -> TaskService | None:
    settings = get_settings()
    if not settings.review_agent_database_url:
        return None
    from review_agent.services.task_store_postgres import PostgresTaskStore

    store = PostgresTaskStore(url=settings.review_agent_database_url)
    return TaskService(store, get_workspace_manager(), settings)


def require_task_service() -> TaskService:
    service = build_task_service()
    if service is None:
        raise RuntimeError("REVIEW_AGENT_DATABASE_URL is required for the worker")
    return service
