"""Celery application. Redis is the broker; PostgreSQL remains the business source of truth."""

from __future__ import annotations

from celery import Celery

from celery.signals import worker_ready

from review_agent.config import get_settings


def _broker_url() -> str:
    settings = get_settings()
    return settings.review_agent_celery_broker or "memory://"


celery_app = Celery("review_agent", broker=_broker_url())
celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "scan-dispatch-pending": {
            "task": "review_agent.scan_dispatch_pending",
            "schedule": 15.0,
        }
    },
)

# Register task modules.
from review_agent.worker import tasks as _tasks  # noqa: E402,F401


@worker_ready.connect
def _scan_on_boot(**_kwargs) -> None:
    from review_agent.worker.tasks import worker_startup_scan

    worker_startup_scan()
