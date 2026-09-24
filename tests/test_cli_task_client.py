from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from review_agent.api.app import create_app
from review_agent.cli import main
from review_agent.clients.task_api import TaskApiClient, match_workspace, unregistered_hint
from review_agent.harness.task_store import ApprovalRecord
from review_agent.harness.models import RunStatus
from review_agent.services.review_store import ReviewStore
from tests.test_task_service import _client, _service


def _api_client(tmp_path: Path, service=None) -> tuple[TaskApiClient, object]:
    service = service or _service(tmp_path)
    app = create_app(store=ReviewStore(tmp_path / "reviews.sqlite3"), task_service=service)
    http = TestClient(app)
    return TaskApiClient(base_url="http://testserver", http=http), service


def test_unregistered_path_does_not_post_to_api(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client, service = _api_client(tmp_path)
    calls: list[str] = []
    original = client.http.request

    def tracking_request(method, url, *args, **kwargs):
        calls.append(f"{method} {url}")
        return original(method, url, *args, **kwargs)

    client.http.request = tracking_request  # type: ignore[method-assign]
    other = tmp_path / "not-registered"
    other.mkdir()
    code = main(["agent", str(other)], api_client=client)
    captured = capsys.readouterr()
    assert code == 1
    assert "workspace_registry.json" in captured.out
    assert "not-registered" in captured.out.replace("\n", "")
    assert not any("/api/sessions" in item and item.startswith("POST") for item in calls)


def test_match_workspace_by_path_and_id(tmp_path: Path) -> None:
    listed = [{"workspace_id": "demo", "path": str(tmp_path / "ws"), "display_name": "Demo"}]
    (tmp_path / "ws").mkdir()
    assert match_workspace(listed, path=tmp_path / "ws")["workspace_id"] == "demo"
    assert match_workspace(listed, workspace_id="demo")["workspace_id"] == "demo"
    assert match_workspace(listed, path=tmp_path / "nope") is None
    assert "workspace_registry.json" in unregistered_hint(tmp_path / "nope")


def test_cli_and_http_see_same_run_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service(tmp_path)
    http_client = _client(tmp_path, service)
    api_client, _ = _api_client(tmp_path, service)
    workspace = service.workspace_manager.list_workspaces()[0].path

    session = api_client.create_session("demo")
    created = api_client.create_run(session["session_id"], "say done", idempotency_key="same-run")
    run_id = created["run_id"]
    service.run_once(run_id, approver=lambda _req: True)
    prepared = service.workspace_manager.get_run(run_id)
    assert prepared is not None
    (prepared.artifacts_root / "change.patch").write_text("diff --git a/x b/x\n", encoding="utf-8")
    service._register_run_artifacts(run_id)

    via_cli = api_client.get_run(run_id)
    via_http = http_client.get(f"/api/runs/{run_id}").json()
    assert via_cli["run_id"] == via_http["run_id"] == run_id
    assert via_cli["status"] == via_http["status"]
    assert via_cli.get("usage") == via_http.get("usage")
    assert via_cli.get("verification") == via_http.get("verification")
    artifacts = api_client.list_artifacts(run_id)
    http_artifacts = http_client.get(f"/api/runs/{run_id}/artifacts").json()
    assert [item["artifact_id"] for item in artifacts] == [item["artifact_id"] for item in http_artifacts]
    change = next(item for item in artifacts if item["summary"] == "change.patch")
    text = api_client.get_artifact_text(run_id, change["artifact_id"])
    assert "diff --git" in text

    monkeypatch.setattr("builtins.input", lambda _: "/exit")
    assert main(["agent", str(workspace), "--session", session["session_id"]], api_client=api_client) == 0


def test_sse_resume_dedupes_seq(tmp_path: Path) -> None:
    service = _service(tmp_path)
    api_client, _ = _api_client(tmp_path, service)
    session = api_client.create_session("demo")
    run_id = api_client.create_run(session["session_id"], "say done", idempotency_key="sse-cli")["run_id"]
    service.run_once(run_id, approver=lambda _req: True)

    seen: set[tuple[str, int]] = set()
    first = list(api_client.iter_events(run_id, after_seq=0, seen=seen))
    seqs = [event["seq"] for event in first if event.get("seq") is not None]
    assert seqs
    assert seqs == sorted(set(seqs))
    after = seqs[0]
    resumed = list(api_client.iter_events(run_id, after_seq=after, seen=seen))
    resumed_seqs = [event["seq"] for event in resumed if event.get("seq") is not None]
    assert after not in resumed_seqs
    assert all(seq > after for seq in resumed_seqs)


def test_cancel_stays_cancelling_until_service_confirms(tmp_path: Path) -> None:
    service = _service(tmp_path)
    api_client, _ = _api_client(tmp_path, service)
    session = api_client.create_session("demo")
    run_id = api_client.create_run(session["session_id"], "queued only", idempotency_key="cancel")["run_id"]
    payload = api_client.cancel_run(run_id)
    assert payload["cancel_requested"] is True
    assert payload["status"] != "cancelled"
    fetched = api_client.get_run(run_id)
    assert fetched["cancel_requested"] is True
    assert fetched["status"] != "cancelled"


def test_approval_and_resume_carry_intent_and_revision(tmp_path: Path) -> None:
    service = _service(tmp_path)
    api_client, _ = _api_client(tmp_path, service)
    session = api_client.create_session("demo")
    run_id = api_client.create_run(session["session_id"], "need review", idempotency_key="appr")["run_id"]
    revision = api_client.get_run(run_id)["workspace_snapshot"]["source_revision"]
    service.store.create_approval(
        ApprovalRecord(
            approval_id="intent:c1",
            run_id=run_id,
            intent={"name": "apply_patch", "arguments": {"path": "a.py"}},
            param_summary="path",
            workspace_revision=revision,
            call_id="c1",
        )
    )
    listed = api_client.get_run(run_id)
    pending = listed["pending_approval"]
    assert pending["approval_id"] == "intent:c1"
    assert pending["intent"]["name"] == "apply_patch"
    assert pending["workspace_revision"] == revision
    decided = api_client.decide_approval("intent:c1", "allow")
    assert decided.get("ok") is True
    assert api_client.get_run(run_id)["task_mode"] == "develop"

    service.store.update_run(
        run_id,
        status=RunStatus.NEEDS_ATTENTION,
        workspace_revision=revision,
        attention={"reason": "unknown command", "call_id": "c2", "affected_files": ["a.py"]},
    )
    resumed = api_client.resume_run(
        run_id,
        action="end_task",
        call_id="c2",
        workspace_revision=revision,
    )
    assert resumed["status"] in {"cancelled", "failed"}


def test_cli_explicit_mode_switch_creates_new_development_run(tmp_path: Path, monkeypatch, capsys) -> None:
    from review_agent.harness.models import ModelTurnResult
    from tests.test_coding_runtime import FakeAsyncModelClient, PLAN_JSON, _tool

    patch = "*** Begin Patch\n*** Add File: created.txt\n+implemented\n*** End Patch"
    model = FakeAsyncModelClient([ModelTurnResult(content=PLAN_JSON),
        ModelTurnResult(tool_calls=[_tool("apply_patch", {"patch": patch})]), ModelTurnResult(content="done")])
    service = _service(tmp_path, model)
    client, _ = _api_client(tmp_path, service)
    inputs = iter(["plan the change", "/mode develop", "implement it", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    monkeypatch.setattr("review_agent.cli._consume_events", lambda _client, _console, rid, _seen: service.run_once(rid))
    assert main(["agent", "--workspace", "demo", "--mode", "plan", "--reviewer", "off"], api_client=client) == 0
    sessions, _ = service.store.list_sessions()
    runs = service.store.list_runs(sessions[0].session_id)
    assert [run.task_mode for run in runs] == ["plan", "develop"]
    assert [run.status.value for run in runs] == ["succeeded", "succeeded"]
    assert not (service.workspace_manager.get_run(runs[0].run_id).source_root / "created.txt").exists()
    assert (service.workspace_manager.get_run(runs[1].run_id).source_root / "created.txt").read_text() == "implemented\n"
    assert "mode=plan" in capsys.readouterr().out


def test_cli_active_mode_cannot_be_changed_or_appended_as_develop(tmp_path: Path, monkeypatch, capsys) -> None:
    import pytest
    from review_agent.cli import _submit_requirement
    from review_agent.clients.task_api import TaskApiError

    client, service = _api_client(tmp_path)
    sid = client.create_session("demo")["session_id"]
    rid = client.create_run(sid, "plan", task_mode="plan", idempotency_key="plan")["run_id"]
    inputs = iter(["/mode develop", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(inputs))
    assert main(["agent", "--workspace", "demo", "--session", sid, "--mode", "plan"], api_client=client) == 0
    assert "Finish or cancel" in capsys.readouterr().out
    with pytest.raises(TaskApiError, match="frozen"):
        _submit_requirement(client, sid, "implement", active_run_id=rid, reviewer="default", task_mode="develop")
    assert service.store.get_run(rid).task_mode == "plan"


def test_http_client_review_target_roundtrip(tmp_path: Path) -> None:
    client, _ = _api_client(tmp_path)
    sid = client.create_session("demo")["session_id"]
    rid = client.create_run(sid, "inspect", task_mode="review", idempotency_key="scope",
        review_target={"kind": "paths", "paths": ["./readme.txt"], "focus": "requirements"})["run_id"]
    loaded = client.get_run(rid)
    assert loaded["task_mode"] == "review"
    assert loaded["review_target"]["paths"] == ["readme.txt"]


def test_cli_displays_actual_review_report_and_event_scope(tmp_path: Path, capsys) -> None:
    import json
    from review_agent.cli import _consume_events, _PlainConsole, _print_run_summary
    from review_agent.harness.models import ModelTurnResult, Usage
    from tests.test_coding_runtime import FakeAsyncModelClient

    finding = {"finding_id": "doc-1", "file_path": "readme.txt", "start_line": 1, "end_line": 1,
               "severity": "low", "category": "correctness", "title": "Missing instructions",
               "evidence": "Only 'ok' is present", "explanation": "No usage instructions", "suggestion": "Add usage",
               "confidence": 0.9, "is_blocking": False}
    model = FakeAsyncModelClient([ModelTurnResult(content="Inspect the README"),
        ModelTurnResult(content=json.dumps({"findings": [finding]}), usage=Usage(profile_id="test", input_tokens=20, output_tokens=None))])
    service = _service(tmp_path, model)
    client, _ = _api_client(tmp_path, service)
    sid = client.create_session("demo")["session_id"]
    rid = client.create_run(sid, "review docs", task_mode="review", idempotency_key="report",
        review_target={"kind": "paths", "paths": ["readme.txt"], "focus": "usability"})["run_id"]
    service.run_once(rid)
    report = client.get_run(rid)
    assert report["status"] == "succeeded"
    assert report["review"]["findings"][0]["title"] == "Missing instructions"
    _consume_events(client, _PlainConsole(), rid, set())
    _print_run_summary(_PlainConsole(), report)
    output = capsys.readouterr().out
    assert "target=paths" in output and "focus=usability" in output
    assert "Evidence: Only 'ok' is present" in output
    assert "in=20 out=unknown" in output
    assert "Read records:" in output and "readme.txt" in output
    assert "findings do not trigger automatic fixes" in output
    assert "verification=not_applicable" in output

    # Outdated verification must never be presented as current, even in a status query.
    report["verification"] = {"status": "passed", "workspace_revision": "older"}
    report["review"]["workspace_revision"] = "older"
    report["review"]["dispositions"] = [{"finding_id": "doc-1", "status": "not_adopted", "reason": "example-only"}]
    _print_run_summary(_PlainConsole(), report)
    stale = capsys.readouterr().out
    assert "verification=stale (current code unverified)" in stale
    assert "Review is stale" in stale
    assert "disposition=not_adopted reason=example-only" in stale
