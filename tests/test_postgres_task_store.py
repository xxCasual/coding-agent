from __future__ import annotations

import os
import threading

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
pytest.importorskip("psycopg")

from sqlalchemy.exc import OperationalError

from review_agent.db.engine import create_db_engine
from review_agent.db.models import Base
from review_agent.services.task_store_postgres import PostgresTaskStore

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


@pytest.fixture()
def pg_store() -> PostgresTaskStore:
    url = _connect_or_skip()
    engine = create_db_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return PostgresTaskStore(engine)


def test_postgres_idempotent_create_and_cross_connection(pg_store: PostgresTaskStore) -> None:
    session = pg_store.create_session("demo")
    first = pg_store.create_run_bundle(
        session.session_id,
        "fix tests",
        idempotency_key="same",
    )
    second = pg_store.create_run_bundle(
        session.session_id,
        "fix tests",
        idempotency_key="same",
    )
    assert first.run_id == second.run_id

    other = PostgresTaskStore(url=DATABASE_URL)
    loaded = other.get_run(first.run_id)
    assert loaded is not None
    assert loaded.requirement == "fix tests"
    messages = other.list_messages(session.session_id)
    assert any(item.content == "fix tests" for item in messages)
    events = other.list_events(first.run_id)
    assert events and events[0].type == "run.created"
    assert events[0] is not first  # different Python object


def test_postgres_concurrent_same_idempotency_one_run(pg_store: PostgresTaskStore) -> None:
    session = pg_store.create_session("demo")
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        store = PostgresTaskStore(url=DATABASE_URL)
        try:
            run = store.create_run_bundle(session.session_id, "fix tests", idempotency_key="race")
            results.append(run.run_id)
        except BaseException as exc:  # noqa: BLE001 - collect either success or mapped conflict
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors, errors
    assert len(set(results)) == 1
    runs = pg_store.list_runs(session.session_id)
    assert len(runs) == 1


def test_postgres_two_active_runs_rejected(pg_store: PostgresTaskStore) -> None:
    session = pg_store.create_session("demo")
    pg_store.create_run_bundle(session.session_id, "first", idempotency_key="a")
    with pytest.raises(RuntimeError, match="active run"):
        pg_store.create_run_bundle(session.session_id, "second", idempotency_key="b")


def test_postgres_event_resume_after_seq(pg_store: PostgresTaskStore) -> None:
    from review_agent.harness.models import AgentEvent

    session = pg_store.create_session("demo")
    run = pg_store.create_run_bundle(session.session_id, "fix tests", idempotency_key="seq")
    pg_store.append_event(AgentEvent(type="model.done", message="turn", run_id=run.run_id))
    pg_store.append_event(AgentEvent(type="run.succeeded", message="ok", run_id=run.run_id))

    other = PostgresTaskStore(url=DATABASE_URL)
    all_events = other.list_events(run.run_id)
    assert [event.seq for event in all_events] == [1, 2, 3]
    assert [event.type for event in all_events] == ["run.created", "model.done", "run.succeeded"]

    resumed = other.list_events(run.run_id, after_seq=1)
    assert [event.seq for event in resumed] == [2, 3]
    assert [event.type for event in resumed] == ["model.done", "run.succeeded"]
    assert all(event is not all_events[index] for index, event in enumerate(resumed, start=1))


def test_postgres_claim_run_is_exclusive(pg_store: PostgresTaskStore) -> None:
    session = pg_store.create_session("demo")
    run = pg_store.create_run_bundle(session.session_id, "fix tests", idempotency_key="lease")
    first = pg_store.claim_run(run.run_id, "worker-a", lease_seconds=60)
    assert first is not None
    assert first.owner_id == "worker-a"
    second = pg_store.claim_run(run.run_id, "worker-b", lease_seconds=60)
    assert second is None
    pg_store.release_run(run.run_id, "worker-a")
    third = pg_store.claim_run(run.run_id, "worker-b", lease_seconds=60)
    assert third is not None
    assert third.owner_id == "worker-b"


def test_postgres_dispatch_pending_lifecycle(pg_store: PostgresTaskStore) -> None:
    session = pg_store.create_session("demo")
    run = pg_store.create_run_bundle(session.session_id, "fix tests", idempotency_key="pending")
    assert run.dispatch_pending is True
    pending_ids = {item.run_id for item in pg_store.list_dispatch_pending()}
    assert run.run_id in pending_ids
    claimed = pg_store.claim_run(run.run_id, "worker-a", lease_seconds=60)
    assert claimed is not None
    assert claimed.dispatch_pending is False
    assert all(item.run_id != run.run_id for item in pg_store.list_dispatch_pending())


def test_postgres_review_json_roundtrip(pg_store: PostgresTaskStore) -> None:
    session = pg_store.create_session("demo")
    run = pg_store.create_run(session.session_id, "review me", reviewer="off")
    assert run.review is not None
    assert run.review["mode"] == "off"
    updated = pg_store.update_run(
        run.run_id,
        review={
            "enabled": True,
            "mode": "default",
            "status": "completed",
            "findings": [],
            "dispositions": [],
            "workspace_revision": "abc",
        },
    )
    loaded = pg_store.get_run(run.run_id)
    assert loaded is not None
    assert loaded.review == updated.review
    assert loaded.review["status"] == "completed"


def test_postgres_modes_targets_are_frozen_and_survive_lease(pg_store: PostgresTaskStore) -> None:
    from review_agent.harness.review_target import ReviewTarget

    session = pg_store.create_session("demo")
    target = ReviewTarget(kind="paths", paths=["./app.py"], focus="errors")
    run = pg_store.create_run_bundle(session.session_id, "inspect", idempotency_key="mode",
                                    task_mode="review", review_target=target)
    other = PostgresTaskStore(url=DATABASE_URL)
    loaded = other.get_run(run.run_id)
    assert loaded.task_mode == "review" and loaded.review_target == target
    assert loaded.acceptance.mode == "not_applicable"
    assert other.create_run_bundle(session.session_id, "inspect", idempotency_key="mode",
        task_mode="review", review_target=target).run_id == run.run_id
    with pytest.raises(ValueError, match="idempotency"):
        other.create_run_bundle(session.session_id, "inspect", idempotency_key="mode", task_mode="plan")
    with pytest.raises(ValueError, match="idempotency"):
        other.create_run_bundle(session.session_id, "inspect", idempotency_key="mode", task_mode="review",
                                review_target=target.model_copy(update={"focus": "performance"}))
    claimed = other.claim_run(run.run_id, "mode-worker", lease_seconds=60)
    assert claimed.task_mode == "review" and claimed.review_target == target
    pg_store.update_run(run.run_id, workspace_snapshot={"source_revision": "frozen", "source_run_id": None})
    assert other.get_run(run.run_id).workspace_snapshot["source_revision"] == "frozen"
    with pytest.raises(TypeError):
        other.update_run(run.run_id, task_mode="develop")


def test_postgres_task_mode_migration_preserves_legacy_runs() -> None:
    """Use only the same disposable database required by pg_store."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import IntegrityError

    engine = create_db_engine(_connect_or_skip())
    Base.metadata.drop_all(engine)
    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    command.stamp(config, "base")
    command.upgrade(config, "0004_tool_replay_category")
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO sessions VALUES ('legacy-session', 'demo', now())")
        conn.exec_driver_sql("""
            INSERT INTO runs (run_id, session_id, status, requirement, acceptance, created_at, updated_at)
            VALUES ('legacy-run', 'legacy-session', 'succeeded', 'legacy requirement',
                    '{"mode":"none","checks":[]}'::jsonb, now(), now())
        """)
    command.upgrade(config, "head")
    with engine.connect() as conn:
        row = conn.exec_driver_sql(
            "SELECT task_mode, review_target, requirement FROM runs WHERE run_id='legacy-run'"
        ).one()
        assert tuple(row) == ("develop", None, "legacy requirement")
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0005_run_task_mode"
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE runs SET task_mode='unsupported' WHERE run_id='legacy-run'")
    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.exec_driver_sql("UPDATE runs SET task_mode=NULL WHERE run_id='legacy-run'")
    loaded = PostgresTaskStore(url=DATABASE_URL).get_run("legacy-run")
    assert loaded is not None and loaded.task_mode == "develop" and loaded.review_target is None
    engine.dispose()


def test_postgres_forced_review_call_and_execution_state_roundtrip(pg_store: PostgresTaskStore) -> None:
    from review_agent.harness.models import ToolCall, ToolResult
    from review_agent.harness.reviewer import forced_review_call_id

    session = pg_store.create_session("demo")
    run = pg_store.create_run_bundle(session.session_id, "review", idempotency_key="forced-review")
    call_id = forced_review_call_id(run.run_id, "a" * 24, "b" * 64)
    pg_store.record_tool_execution(run_id=run.run_id, session_id=session.session_id,
        tool_call=ToolCall(name="delegate_review", call_id=call_id, provider_call_id=call_id))
    other = PostgresTaskStore(url=DATABASE_URL)
    for state in ("pending", "awaiting_approval", "executing"):
        pg_store.complete_tool_execution(call_id, None, execution_meta={"status": state})
        saved = other.get_tool_execution(call_id)
        assert saved.result is None and saved.execution_status == state
    pg_store.complete_tool_execution(call_id, ToolResult(call_id=call_id, success=True, summary="reviewed"))
    assert other.get_tool_execution(call_id).result.success
    assert forced_review_call_id(run.run_id, "a" * 24, "b" * 64) == call_id
    assert forced_review_call_id(run.run_id, "a" * 24, "c" * 64) != call_id
