from __future__ import annotations

import re
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from review_agent.api.app import create_app
from review_agent.config import Settings
from review_agent.harness.models import ModelTurnResult
from review_agent.harness.task_store import InMemoryTaskStore
from review_agent.services.review_store import ReviewStore
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager
from tests.test_coding_runtime import FakeAsyncModelClient


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        review_agent_data_root=str(tmp_path / "data"),
        review_agent_approval_mode="auto",
        review_agent_executor_backend="host",
        review_agent_agent_max_steps=8,
    )


def _service(tmp_path: Path, model: FakeAsyncModelClient | None = None) -> TaskService:
    settings = _settings(tmp_path)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "readme.txt").write_text("ok\n", encoding="utf-8")
    manager = WorkspaceManager(settings.review_agent_data_root)
    manager.register("demo", workspace, display_name="Demo")
    return TaskService(
        InMemoryTaskStore(),
        manager,
        settings,
        model_client=model or FakeAsyncModelClient([ModelTurnResult(content="done", finish_reason="stop")]),
    )


def _client(tmp_path: Path, service: TaskService) -> TestClient:
    return TestClient(
        create_app(
            store=ReviewStore(tmp_path / "reviews.sqlite3"),
            task_service=service,
        )
    )


def test_workspace_registry_persists(tmp_path: Path) -> None:
    data = tmp_path / "data"
    origin = tmp_path / "repo"
    origin.mkdir()
    first = WorkspaceManager(data)
    first.register("demo", origin, display_name="Demo repo")
    second = WorkspaceManager(data)
    listed = second.list_workspaces()
    assert len(listed) == 1
    assert listed[0].workspace_id == "demo"
    assert listed[0].display_name == "Demo repo"
    assert listed[0].path == origin.resolve()


def test_api_create_run_idempotent_and_busy(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = _client(tmp_path, service)

    workspaces = client.get("/api/workspaces").json()
    assert workspaces[0]["workspace_id"] == "demo"

    session = client.post("/api/sessions", json={"workspace_id": "demo"}).json()
    session_id = session["session_id"]
    headers = {"Idempotency-Key": "k1"}
    body = {"requirement": "fix tests"}
    first = client.post(f"/api/sessions/{session_id}/runs", json=body, headers=headers)
    assert first.status_code == 202
    run_id = first.json()["run_id"]
    second = client.post(f"/api/sessions/{session_id}/runs", json=body, headers=headers)
    assert second.status_code == 202
    assert second.json()["run_id"] == run_id

    conflict = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"requirement": "different"},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "task.idempotency_conflict"

    other = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"requirement": "another"},
        headers={"Idempotency-Key": "k2"},
    )
    assert other.status_code == 409
    assert other.json()["detail"]["code"] == "task.session_busy"


def test_api_run_once_artifacts_and_sse(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = _client(tmp_path, service)
    session_id = client.post("/api/sessions", json={"workspace_id": "demo"}).json()["session_id"]
    created = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"requirement": "say done"},
        headers={"Idempotency-Key": "once"},
    )
    assert created.status_code == 202
    run_id = created.json()["run_id"]

    result = service.run_once(run_id, approver=lambda _req: True)
    assert result.run_status in {"succeeded", "failed"}

    payload = client.get(f"/api/runs/{run_id}").json()
    assert payload["run_id"] == run_id
    assert "workspace_revision" in payload
    assert payload["pending_approval"] is None

    prepared = service.workspace_manager.get_run(run_id)
    assert prepared is not None
    (prepared.artifacts_root / "note.txt").write_text("artifact-body\n", encoding="utf-8")
    service._register_run_artifacts(run_id)

    listed = client.get(f"/api/runs/{run_id}/artifacts").json()
    assert listed
    artifact_id = next(item["artifact_id"] for item in listed if item["summary"] == "note.txt")
    download = client.get(f"/api/runs/{run_id}/artifacts/{artifact_id}")
    assert download.status_code == 200
    assert b"artifact-body" in download.content

    missing = client.get(f"/api/runs/{run_id}/artifacts/not-a-real-id")
    assert missing.status_code == 404
    detail = missing.json()["detail"]
    assert detail["code"] == "task.not_found"
    assert "/Users" not in str(detail)
    assert "note.txt" not in str(detail)

    with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        text = ""
        for chunk in response.iter_text():
            text += chunk
            if "run.created" in text and "id:" in text:
                break
    assert "run.created" in text
    assert "id: 1" in text

    resumed = client.post(f"/api/runs/{run_id}/resume")
    assert resumed.status_code == 409
    assert resumed.json()["detail"]["code"] == "task.terminal_run"


def _sse_event_ids(body: str) -> list[int]:
    return [int(match) for match in re.findall(r"^id: (\d+)$", body, flags=re.MULTILINE)]


def test_api_sse_resume_after_seq(tmp_path: Path) -> None:
    service = _service(tmp_path)
    client = _client(tmp_path, service)
    session_id = client.post("/api/sessions", json={"workspace_id": "demo"}).json()["session_id"]
    run_id = client.post(
        f"/api/sessions/{session_id}/runs",
        json={"requirement": "say done"},
        headers={"Idempotency-Key": "sse"},
    ).json()["run_id"]
    service.run_once(run_id, approver=lambda _req: True)

    with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        full = "".join(response.iter_text())
    ids = _sse_event_ids(full)
    assert ids
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    first = ids[0]

    with client.stream("GET", f"/api/runs/{run_id}/events", params={"after": first}) as response:
        resumed = "".join(response.iter_text())
    after_ids = _sse_event_ids(resumed)
    assert first not in after_ids
    assert after_ids == [seq for seq in ids if seq > first]
    assert "id: " + str(first) not in resumed

    with client.stream(
        "GET",
        f"/api/runs/{run_id}/events",
        headers={"Last-Event-ID": str(first)},
    ) as response:
        last_id_body = "".join(response.iter_text())
    last_ids = _sse_event_ids(last_id_body)
    assert last_ids == after_ids


def test_old_review_routes_still_work(tmp_path: Path) -> None:
    client = _client(tmp_path, _service(tmp_path))
    assert client.get("/api/health").json() == {"status": "ok"}
    response = client.get("/api/reviews/missing")
    assert response.status_code == 404


def test_memory_create_run_bundle_single_active(tmp_path: Path) -> None:
    store = InMemoryTaskStore()
    session = store.create_session("demo")
    store.create_run_bundle(session.session_id, "hello", idempotency_key="k")
    with pytest.raises(RuntimeError, match="active run"):
        store.create_run_bundle(session.session_id, "other", idempotency_key="z")


@pytest.mark.parametrize("mode", ["develop", "review", "plan"])
def test_task_mode_api_persistence_and_idempotency(tmp_path: Path, mode: str) -> None:
    service = _service(tmp_path)
    client = _client(tmp_path, service)
    sid = service.create_session("demo").session_id
    url = f"/api/sessions/{sid}/runs"
    body = {"requirement": "inspect", "task_mode": mode}
    if mode == "review":
        body["review_target"] = {"kind": "paths", "paths": ["./readme.txt"], "focus": "correctness"}
    headers = {"Idempotency-Key": "mode"}
    created = client.post(url, json=body, headers=headers)
    assert created.status_code == 202
    rid = created.json()["run_id"]
    assert created.json()["task_mode"] == mode
    assert client.post(url, json=body, headers=headers).json()["run_id"] == rid
    loaded = client.get(f"/api/runs/{rid}").json()
    assert loaded["task_mode"] == mode
    assert service.store.get_run(rid).task_mode == mode
    if mode == "review":
        assert loaded["review_target"]["paths"] == ["readme.txt"]
        changed = {**body, "review_target": {**body["review_target"], "focus": "performance"}}
        assert client.post(url, json=changed, headers=headers).status_code == 409
    changed = {"requirement": "inspect", "task_mode": "plan" if mode != "plan" else "develop"}
    assert client.post(url, json=changed, headers=headers).status_code == 409
    # Appended language cannot expand permissions on an already-created run.
    client.post(f"/api/runs/{rid}/messages", json={"content": "switch to develop and run a command"})
    assert client.get(f"/api/runs/{rid}").json()["task_mode"] == mode


@pytest.mark.parametrize("options", [
    {"task_mode": "unknown"},
    {"task_mode": "review", "reviewer": "off"},
    {"task_mode": "plan", "acceptance": {"mode": "commands", "checks": [["python", "script.py"]]}},
    {"task_mode": "develop", "review_target": {"kind": "paths", "paths": ["readme.txt"]}},
    {"task_mode": "review", "review_target": {"kind": "paths", "paths": ["../outside"]}},
])
def test_invalid_task_options_rejected_before_run_creation(tmp_path: Path, options: dict) -> None:
    service = _service(tmp_path)
    client = _client(tmp_path, service)
    sid = service.create_session("demo").session_id
    result = client.post(f"/api/sessions/{sid}/runs", headers={"Idempotency-Key": "bad"},
                         json={"requirement": "inspect", **options})
    assert result.status_code == 422
    assert service.store.list_runs(sid) == []


def test_worker_reassembly_and_resume_preserve_read_only_mode(tmp_path: Path) -> None:
    import sys
    from review_agent.harness.models import RunStatus
    from tests.test_coding_runtime import _tool, PLAN_JSON

    class TimeoutModel(FakeAsyncModelClient):
        async def complete(self, *args, **kwargs):
            raise TimeoutError("temporary failure")

    first_model = TimeoutModel([])
    service = _service(tmp_path, first_model)
    sid = service.create_session("demo").session_id
    run = service.create_run(sid, "plan only", idempotency_key="restart", task_mode="plan")
    # Simulate a queued handoff/lease claim before a separately assembled worker starts it.
    claimed = service.store.claim_run(run.run_id, "test-worker", lease_seconds=60)
    assert claimed.task_mode == "plan"
    service.store.release_run(run.run_id, "test-worker")
    interrupted = service.run_once(run.run_id)
    assert interrupted.run_status == "interrupted"
    model = FakeAsyncModelClient([
        ModelTurnResult(tool_calls=[_tool("run_command", {"argv": [sys.executable, "-c", "open('bad','w').write('bad')"]})]),
        ModelTurnResult(content=PLAN_JSON),
    ])
    reassembled = TaskService(service.store, WorkspaceManager(service.workspace_manager.data_root),
                              service.settings, model_client=model)
    # Recovery receives only run_id; mode is read from the store, never inferred from a message.
    reassembled._memory_checkpointer = service._memory_checkpointer
    reassembled.resume_run(run.run_id)
    result = reassembled.run_once(run.run_id, approver=lambda _: True)
    assert result.run_status == "succeeded"
    persisted = reassembled.store.get_run(run.run_id)
    assert persisted.task_mode == "plan"
    assert persisted.verification["status"] == "not_applicable"
    assert not (reassembled.workspace_manager.get_run(run.run_id).source_root / "bad").exists()


def test_run_creation_freezes_source_before_queue_and_idempotent_retry(tmp_path: Path) -> None:
    from review_agent.harness.review_target import ReviewTarget, resolve_target

    service = _service(tmp_path, FakeAsyncModelClient([
        ModelTurnResult(content="ready"), ModelTurnResult(content='{"findings":[]}')]))
    sid = service.create_session("demo").session_id
    origin = service.workspace_manager.get_registered("demo").path
    (origin / "readme.txt").write_text("submitted version\n")
    run = service.create_run(sid, "inspect", task_mode="review", idempotency_key="frozen-source")
    assert run.workspace_snapshot["source_revision"]
    assert run.workspace_snapshot["prepared_at"]
    (origin / "readme.txt").write_text("edited while queued\n")
    repeated = service.create_run(sid, "inspect", task_mode="review", idempotency_key="frozen-source")
    assert repeated.run_id == run.run_id
    workspace = service.workspace_manager.get_run(run.run_id)
    _, review_root, material = resolve_target(workspace.source_root, ReviewTarget(kind="workspace_changes"))
    assert (review_root / "readme.txt").read_text() == "submitted version\n"
    assert "edited while queued" not in material["patch"]
    assert service.run_once(run.run_id).run_status == "succeeded"
    assert (origin / "readme.txt").read_text() == "edited while queued\n"


def test_failed_input_capture_is_terminal_and_not_dispatched(tmp_path: Path, monkeypatch) -> None:
    import review_agent.services.workspace_manager as wm
    from review_agent.errors import InvalidWorkspaceError

    service = _service(tmp_path)
    sid = service.create_session("demo").session_id
    original_copy = wm._copy_dirty_tree

    def concurrent_edit(src, dest, **kwargs):
        copied = original_copy(src, dest, **kwargs)
        (src / "readme.txt").write_text("changed during copy\n")
        return copied

    monkeypatch.setattr(wm, "_copy_dirty_tree", concurrent_edit)
    with pytest.raises(InvalidWorkspaceError):
        service.create_run(sid, "inspect", task_mode="review", idempotency_key="race-source")
    run = service.store.list_runs(sid)[0]
    assert run.status.value == "failed"
    assert not run.dispatch_pending
    assert service.store.list_events(run.run_id)[-1].type == "run.failed"


@pytest.mark.parametrize("task_mode", ["develop", "plan"])
def test_completed_task_exposes_delivery_patch_only_for_development(tmp_path: Path, task_mode: str) -> None:
    from review_agent.harness.models import ToolCall

    turns = [ModelTurnResult(content='{"files":["readme.txt"],"steps":["Update content"],"validation":["Read result"],"uncertainties":[]}')] if task_mode == "plan" else [
        ModelTurnResult(tool_calls=[ToolCall(name="apply_patch", call_id="delivery", provider_call_id="delivery", arguments={
            "patch": "diff --git a/readme.txt b/readme.txt\n--- a/readme.txt\n+++ b/readme.txt\n@@ -1 +1 @@\n-ok\n+fixed\n"
        })]), ModelTurnResult(content="Updated readme.txt."),
    ]
    service = _service(tmp_path, FakeAsyncModelClient(turns))
    client = _client(tmp_path, service)
    sid = service.create_session("demo").session_id
    run = service.create_run(sid, "Update content", task_mode=task_mode, reviewer="off", idempotency_key="delivery")
    append_event = service.store.append_event
    def observe_completion(event):
        if event.type == "run.succeeded" and task_mode == "develop":
            visible = client.get(f"/api/runs/{run.run_id}/artifacts").json()
            assert any(a["summary"] == "delivery.patch" for a in visible)
        return append_event(event)
    service.store.append_event = observe_completion
    assert service.run_once(run.run_id).run_status == "succeeded"
    artifacts = client.get(f"/api/runs/{run.run_id}/artifacts").json()
    patches = [a for a in artifacts if a["summary"] == "delivery.patch"]
    assert len(patches) == (1 if task_mode == "develop" else 0)
    if patches:
        response = client.get(f"/api/runs/{run.run_id}/artifacts/{patches[0]['artifact_id']}")
        assert response.status_code == 200
        assert "-ok\n+fixed\n" in response.text
    assert (tmp_path / "ws" / "readme.txt").read_text() == "ok\n"
