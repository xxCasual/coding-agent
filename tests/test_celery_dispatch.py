from __future__ import annotations

import os
from pathlib import Path

import pytest

from review_agent.config import Settings
from review_agent.errors import RunLeaseHeldError, RunLockHeldError
from review_agent.harness.models import ModelTurnResult
from review_agent.harness.task_store import InMemoryTaskStore
from review_agent.services.dispatch import CeleryPublisher, NoOpPublisher
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager
from review_agent.worker.locks import run_file_lock
from tests.test_coding_runtime import FakeAsyncModelClient


def _settings(tmp_path: Path, **kwargs) -> Settings:
    data = {
        "review_agent_data_root": str(tmp_path / "data"),
        "review_agent_approval_mode": "auto",
        "review_agent_executor_backend": "host",
        "review_agent_agent_max_steps": 4,
        "review_agent_lease_seconds": 30,
    }
    data.update(kwargs)
    return Settings(**data)


def _service(tmp_path: Path, *, publisher=None, broker: str = "") -> TaskService:
    settings = _settings(tmp_path, review_agent_celery_broker=broker)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    manager = WorkspaceManager(settings.review_agent_data_root)
    manager.register("demo", workspace, display_name="Demo")
    return TaskService(
        InMemoryTaskStore(),
        manager,
        settings,
        model_client=FakeAsyncModelClient([ModelTurnResult(content="done", finish_reason="stop")]),
        publisher=publisher if publisher is not None else NoOpPublisher(),
    )


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[str] = []

    def publish(self, run_id: str) -> None:
        self.published.append(run_id)


def test_create_run_publishes_when_broker_configured(tmp_path: Path) -> None:
    publisher = RecordingPublisher()
    service = _service(tmp_path, publisher=publisher, broker="redis://localhost:6379/0")
    session = service.create_session("demo")
    run = service.create_run(session.session_id, "fix", idempotency_key="k1")
    assert publisher.published == [run.run_id]
    assert run.dispatch_pending is False


def test_duplicate_claim_is_lease_held(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_session("demo")
    run = service.create_run(session.session_id, "fix", idempotency_key="k2")
    claimed = service.store.claim_run(run.run_id, "w1", lease_seconds=30)
    assert claimed is not None
    with pytest.raises(RunLeaseHeldError) as exc:
        service.run_once(run.run_id, owner_id="w2")
    assert exc.value.code == "task.lease_held"


def test_file_lock_maps_to_lock_held(tmp_path: Path) -> None:
    service = _service(tmp_path)
    session = service.create_session("demo")
    run = service.create_run(session.session_id, "fix", idempotency_key="k-lock")
    with run_file_lock(service.workspace_manager.data_root, run.run_id):
        with pytest.raises(RunLockHeldError) as exc:
            service.run_once(run.run_id, owner_id="w-lock")
        assert exc.value.code == "task.lock_held"


def test_scan_dispatch_pending_republishes_three_times(tmp_path: Path) -> None:
    from review_agent.harness.models import RunStatus

    publisher = RecordingPublisher()
    service = _service(tmp_path, publisher=publisher, broker="redis://localhost:6379/0")
    runs = []
    for index in range(3):
        session = service.create_session("demo")
        run = service.create_run(session.session_id, f"fix-{index}", idempotency_key=f"scan-{index}")
        service.store.update_run(run.run_id, status=RunStatus.SUCCEEDED, dispatch_pending=True)
        runs.append(run.run_id)
    count = service.scan_dispatch_pending()
    assert count == 3
    assert publisher.published[-3:] == runs
    for run_id in runs:
        latest = service.store.get_run(run_id)
        assert latest is not None
        assert latest.dispatch_pending is False


def test_celery_publisher_sends_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("celery")
    sent: list[tuple[str, tuple]] = []

    class FakeApp:
        def send_task(self, name: str, args=None, **_kwargs):
            sent.append((name, tuple(args or ())))

    monkeypatch.setattr("review_agent.worker.celery_app.celery_app", FakeApp())
    CeleryPublisher().publish("run-abc")
    assert sent == [("review_agent.execute_run", ("run-abc",))]


def _redis_available() -> bool:
    try:
        import redis
    except ImportError:
        return False
    url = os.environ.get("REVIEW_AGENT_CELERY_BROKER", "redis://localhost:6379/0")
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=1)
        return bool(client.ping())
    except Exception:  # noqa: BLE001 — probe only; missing redis/broker is skip
        return False


@pytest.mark.skipif(not _redis_available(), reason="Redis broker not running")
def test_celery_send_task_reaches_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("celery")
    from review_agent.worker.celery_app import celery_app

    monkeypatch.setenv("REVIEW_AGENT_CELERY_BROKER", os.environ.get("REVIEW_AGENT_CELERY_BROKER", "redis://localhost:6379/0"))
    result = celery_app.send_task("review_agent.scan_dispatch_pending")
    assert result.id
