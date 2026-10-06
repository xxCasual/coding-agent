"""No-key smoke against real PostgreSQL, Redis, HTTP API and a prefork worker.

The database must be empty and disposable. Only the model decisions are scripted;
approval/checkpoint persistence, processes, files, commands and delivery are real.
Run after starting PostgreSQL/Redis; configure REVIEW_AGENT_DATABASE_URL and
REVIEW_AGENT_CELERY_BROKER. This never drops tables or deletes a database.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import inspect

from review_agent.db.engine import create_db_engine
from review_agent.harness.models import ModelTurnResult, ToolCall
from review_agent.services.graph_checkpointer import setup_postgres_checkpointer
from review_agent.services.workspace_manager import WorkspaceManager

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'from fastapi import FastAPI\n\napp = FastAPI()\n\n@app.get("/health")\ndef health():\n    return {"ok": False}\n'
PATCH = '''diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -4,4 +4,4 @@
 
 @app.get("/health")
 def health():
-    return {"ok": False}
+    return {"ok": True}
'''


class ScriptedModel:
    """Decisions depend on persisted messages so worker restarts are supported."""

    async def complete(self, messages, tools=None, **kwargs):
        seen = {m.provider_call_id for m in messages if m.role == "tool"}
        if "smoke-patch" in seen:
            return ModelTurnResult(content="Health fixed; run the frozen acceptance.", finish_reason="stop")
        if "smoke-read" in seen:
            call = ToolCall(name="apply_patch", arguments={"patch": PATCH}, call_id="smoke-patch", provider_call_id="smoke-patch")
        else:
            call = ToolCall(name="read_file", arguments={"path": "app.py"}, call_id="smoke-read", provider_call_id="smoke-read")
        return ModelTurnResult(tool_calls=[call], finish_reason="tool_calls")


def worker_main():
    from celery.signals import worker_process_init
    from review_agent.services.factory import build_task_service
    from review_agent.worker.celery_app import celery_app

    @worker_process_init.connect(weak=False)
    def inject_scripted_model(**kwargs):
        build_task_service.cache_clear()
        service = build_task_service()
        service.model_client = ScriptedModel()

    celery_app.worker_main(["worker", "--pool=prefork", "--concurrency=1", "--loglevel=WARNING", "--without-gossip", "--without-mingle"])


def stop(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def wait_for(action, predicate, timeout=90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = action()
            if predicate(last):
                return last
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError):
            pass
        time.sleep(0.2)
    raise AssertionError(f"Timed out; last value: {last}")


def smoke(report):
    database = os.environ.get("REVIEW_AGENT_DATABASE_URL", "")
    broker = os.environ.get("REVIEW_AGENT_CELERY_BROKER", "")
    assert database and broker, "Configure an empty disposable PostgreSQL database and Redis broker"
    engine = create_db_engine(database)
    assert not inspect(engine).get_table_names(), "Refusing a non-empty database; create a new smoke database"
    engine.dispose()

    with tempfile.TemporaryDirectory(prefix="fullstack-smoke-") as tmp:
        base = Path(tmp)
        repo = base / "repo"
        repo.mkdir()
        (repo / "app.py").write_text(SOURCE)
        (repo / "test_app.py").write_text('from fastapi.testclient import TestClient\nfrom app import app\n\ndef test_health():\n    assert TestClient(app).get("/health").json() == {"ok": True}\n')
        git = ["git", "-c", "user.name=smoke", "-c", "user.email=smoke@example.com"]
        for args in [["init", "-q"], ["add", "app.py", "test_app.py"], ["commit", "-qm", "smoke fixture"]]:
            subprocess.run([*git, *args], cwd=repo, check=True)
        manager = WorkspaceManager(base / "data")
        manager.register("smoke-fastapi", repo, display_name="Disposable FastAPI fixture")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ)
        for name in list(env):
            if name.endswith(("API_KEY", "TOKEN")):
                env.pop(name)
        env.update({"REVIEW_AGENT_DATA_ROOT": str(base / "data"), "REVIEW_AGENT_EXECUTOR_BACKEND": "host", "REVIEW_AGENT_APPROVAL_MODE": "confirm", "REVIEW_AGENT_AGENT_MAX_STEPS": "8", "REVIEW_AGENT_SKILLS_ENABLED": "false", "REVIEW_AGENT_MCP_SERVERS": "[]"})
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=ROOT, env=env, check=True)
        asyncio.run(setup_postgres_checkpointer(database))
        api = worker = None
        logs = []

        def launch(kind):
            stream = (base / f"{kind}-{len(logs)}.log").open("w")
            logs.append(stream)
            command = [sys.executable, str(Path(__file__).resolve()), "--worker"] if kind == "worker" else [sys.executable, "-m", "uvicorn", "review_agent.api.app:app", "--host", "127.0.0.1", "--port", str(port)]
            return subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=subprocess.STDOUT)

        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False)

        def call(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        try:
            api = launch("api")
            wait_for(lambda: call("GET", "/api/health"), lambda x: x["status"] == "ok")
            web = client.get("/")
            assert web.status_code == 200 and 'id="review-root"' in web.text
            session = call("POST", "/api/sessions", json={"workspace_id": "smoke-fastapi"})
            body = {"requirement": "Fix GET /health to return ok true", "reviewer": "off", "acceptance": {"mode": "commands", "checks": [[sys.executable, "-m", "pytest", "-q"]]}}
            path = f"/api/sessions/{session['session_id']}/runs"
            created = call("POST", path, json=body, headers={"Idempotency-Key": "fullstack-smoke"})
            assert call("POST", path, json=body, headers={"Idempotency-Key": "fullstack-smoke"})["run_id"] == created["run_id"]
            run_id = created["run_id"]
            run_path = f"/api/runs/{run_id}"
            assert call("GET", run_path)["status"] == "queued"
            worker = launch("worker")
            waiting = wait_for(lambda: call("GET", run_path), lambda x: x["status"] == "waiting_approval")
            stop(worker)
            worker = None
            stop(api)
            api = launch("api")
            reloaded = wait_for(lambda: call("GET", run_path), lambda x: x["status"] == "waiting_approval")
            assert reloaded["pending_approval_id"] == waiting["pending_approval_id"]
            assert (repo / "app.py").read_text() == SOURCE
            call("POST", f"/api/approvals/{reloaded['pending_approval_id']}/decision", json={"decision": "allow"})
            worker = launch("worker")
            approved_ids = {reloaded["pending_approval_id"]}

            def load_and_approve():
                run = call("GET", run_path)
                approval = run.get("pending_approval_id")
                if run["status"] == "waiting_approval" and approval and approval not in approved_ids:
                    call("POST", f"/api/approvals/{approval}/decision", json={"decision": "allow"})
                    approved_ids.add(approval)
                return run

            final = wait_for(load_and_approve, lambda x: x["status"] in {"succeeded", "failed", "needs_attention", "interrupted"})
            assert final["status"] == "succeeded", final
            assert final["verification"]["status"] == "passed"
            artifacts = call("GET", run_path + "/artifacts")
            patch = next(item for item in artifacts if item["summary"] == "delivery.patch")
            downloaded = client.get(run_path + f"/artifacts/{patch['artifact_id']}")
            downloaded.raise_for_status()
            assert '+    return {"ok": True}' in downloaded.text
            assert hashlib.sha256(downloaded.content).hexdigest() == patch["content_hash"]
            events = client.get(run_path + "/events").text
            ids = [int(line[4:]) for line in events.splitlines() if line.startswith("id: ")]
            assert len(ids) >= 2 and ids == sorted(set(ids))
            after = ids[len(ids) // 2]
            resumed = client.get(run_path + "/events", headers={"Last-Event-ID": str(after)}).text
            resumed_ids = [int(line[4:]) for line in resumed.splitlines() if line.startswith("id: ")]
            assert resumed_ids == [seq for seq in ids if seq > after]
            assert (repo / "app.py").read_text() == SOURCE
            # A duplicate Celery delivery must not reapply a completed patch.
            from review_agent.services.dispatch import CeleryPublisher
            CeleryPublisher().publish(run_id)
            time.sleep(2)
            assert call("GET", run_path)["status"] == "succeeded"
            assert client.get(run_path + f"/artifacts/{patch['artifact_id']}").content == downloaded.content
            stop(api)
            api = launch("api")
            persisted = wait_for(lambda: call("GET", run_path), lambda x: x["status"] == "succeeded")
            result = {"date_utc": datetime.now(timezone.utc).isoformat(), "platform": platform.platform(), "python": platform.python_version(), "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(), "model": "scripted fake (no API calls)", "worker_pool": "prefork, concurrency=1", "run_id": run_id, "run_status": persisted["status"], "verification": persisted["verification"]["status"], "checks": ["empty database migration", "PostgreSQL checkpoint", "HTTP API and Web entry", "Redis dispatch and prefork worker", "idempotent HTTP submission", "approval paused before source modification", "API and worker restart during approval", "checkpoint continuation after approval", "real pytest acceptance", "delivery.patch download and SHA-256", "SSE Last-Event-ID continuation", "duplicate Celery delivery", "terminal state survives API restart", "source repository unchanged"], "model_quality_evaluated": False}
            report.parent.mkdir(parents=True, exist_ok=True)
            result["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
            result["working_tree_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
            report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            print("FULLSTACK SMOKE OK")
        except BaseException:
            for stream in logs:
                stream.flush()
                print(Path(stream.name).read_text()[-10000:], file=sys.stderr)
            raise
        finally:
            stop(worker)
            stop(api)
            client.close()
            for stream in logs:
                stream.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--report", type=Path, default=Path("fullstack-smoke.json"))
    args = parser.parse_args()
    if args.worker:
        worker_main()
    else:
        smoke(args.report)
