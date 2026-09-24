from __future__ import annotations

from typing import Protocol


class DispatchPublisher(Protocol):
    def publish(self, run_id: str) -> None: ...


class NoOpPublisher:
    """Used when Redis/Celery is not configured. Leaves dispatch_pending for later scan."""

    def publish(self, run_id: str) -> None:
        del run_id


class CeleryPublisher:
    def publish(self, run_id: str) -> None:
        from review_agent.worker.celery_app import celery_app

        celery_app.send_task("review_agent.execute_run", args=[run_id])


def publisher_from_settings(settings) -> DispatchPublisher:
    broker = getattr(settings, "review_agent_celery_broker", "") or ""
    if not broker.strip():
        return NoOpPublisher()
    try:
        return CeleryPublisher()
    except Exception:
        return NoOpPublisher()
