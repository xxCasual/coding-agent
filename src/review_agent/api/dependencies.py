from functools import lru_cache

from review_agent.config import get_settings
from review_agent.services.factory import build_task_service, get_workspace_manager
from review_agent.services.review_store import ReviewStore
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager


@lru_cache(maxsize=1)
def get_review_store() -> ReviewStore:
    return ReviewStore(get_settings().review_store_path)


def get_task_service() -> TaskService | None:
    return build_task_service()


__all__ = ["get_review_store", "get_task_service", "get_workspace_manager", "WorkspaceManager"]
