from pathlib import Path
from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from review_agent.config import get_settings
from review_agent.api.dependencies import get_review_store, get_task_service
from review_agent.api.routes import create_router
from review_agent.observability import configure_logging
from review_agent.services.review_store import ReviewStore
from review_agent.services.task_service import TaskService

WEB_DIR = Path(__file__).resolve().parents[1] / "web"


def create_app(
    store: ReviewStore | None = None,
    service=None,
    session_service=None,
    service_factory: Callable | None = None,
    task_service: TaskService | None = None,
) -> FastAPI:
    configure_logging(get_settings().review_agent_log_level)
    app = FastAPI(title="Code Review Agent", version="0.1.0")
    app.include_router(
        create_router(
            store or get_review_store(),
            service,
            session_service,
            service_factory=service_factory,
            task_service=task_service if task_service is not None else get_task_service(),
        )
    )
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    return app


def _review_service_factory():
    from review_agent.services.review_service import ReviewService

    return ReviewService()


def _default_session_service(store):
    from review_agent.services.session_service import SessionService

    return SessionService(store)


_default_store = get_review_store()
try:
    _default_session = _default_session_service(_default_store)
except ImportError:
    _default_session = None

app = create_app(
    service_factory=_review_service_factory,
    store=_default_store,
    session_service=_default_session,
)
