from __future__ import annotations

import os

from review_agent.worker.celery_app import celery_app


def _task_service():
    from review_agent.services.factory import require_task_service

    return require_task_service()


@celery_app.task(name="review_agent.execute_run", bind=True, acks_late=True)
def execute_run_task(self, run_id: str) -> dict[str, str]:
    from review_agent.errors import RunLeaseHeldError, RunLockHeldError

    service = _task_service()
    owner = f"celery:{getattr(self.request, 'id', None) or os.getpid()}"
    try:
        final = service.run_once(run_id, owner_id=owner)
    except (RunLeaseHeldError, RunLockHeldError) as exc:
        return {"status": "skipped", "reason": exc.code, "run_id": run_id}
    return {"status": final.run_status or "unknown", "run_id": run_id}


@celery_app.task(name="review_agent.scan_dispatch_pending")
def scan_dispatch_pending() -> int:
    service = _task_service()
    return service.scan_dispatch_pending()


def worker_startup_scan() -> int:
    """Also invoked when a worker process boots."""
    return scan_dispatch_pending()
