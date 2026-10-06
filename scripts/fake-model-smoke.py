"""No-key smoke run: register a repo, submit a task over HTTP, get patch + acceptance result.

A scripted fake model replaces the LLM, so this checks the platform path only
(workspace copy, tools, acceptance, artifacts, API), not model coding ability.

    uv run python scripts/fake-model-smoke.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from review_agent.api.app import create_app
from review_agent.config import Settings
from review_agent.harness.models import ModelTurnResult, ToolCall
from review_agent.harness.task_store import InMemoryTaskStore
from review_agent.services.review_store import ReviewStore
from review_agent.services.task_service import TaskService
from review_agent.services.workspace_manager import WorkspaceManager

PATCH = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a - b
+    return a + b
"""


class ScriptedModel:
    def __init__(self, turns: list[ModelTurnResult]) -> None:
        self.turns = turns

    async def complete(self, messages, tools=None, **_: object) -> ModelTurnResult:
        return self.turns.pop(0) if self.turns else ModelTurnResult(content="done", finish_reason="stop")


def _call(name: str, arguments: dict, call_id: str) -> ModelTurnResult:
    return ModelTurnResult(
        tool_calls=[ToolCall(name=name, arguments=arguments, call_id=call_id, provider_call_id=call_id)],
        finish_reason="tool_calls",
    )


def _make_repo(root: Path) -> Path:
    repo = root / "sample-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8"
    )
    git = ["git", "-c", "user.email=smoke@example.com", "-c", "user.name=smoke"]
    subprocess.run([*git, "init", "-q"], cwd=repo, check=True)
    subprocess.run([*git, "add", "."], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="coding-agent-smoke-") as tmp:
        root = Path(tmp)
        repo = _make_repo(root)
        settings = Settings(
            review_agent_data_root=str(root / "data"),
            review_agent_approval_mode="auto",
            review_agent_executor_backend="host",
            review_agent_agent_max_steps=8,
        )
        manager = WorkspaceManager(settings.review_agent_data_root)
        manager.register("sample", repo, display_name="Sample repo")
        model = ScriptedModel([
            _call("read_file", {"path": "app.py"}, "read"),
            _call("apply_patch", {"patch": PATCH}, "patch"),
            ModelTurnResult(content="Fixed add().", finish_reason="stop"),
        ])
        service = TaskService(InMemoryTaskStore(), manager, settings, model_client=model)
        client = TestClient(create_app(store=ReviewStore(root / "reviews.sqlite3"), task_service=service))

        session_id = client.post("/api/sessions", json={"workspace_id": "sample"}).json()["session_id"]
        created = client.post(
            f"/api/sessions/{session_id}/runs",
            json={
                "requirement": "Fix add() so test_app.py passes",
                "reviewer": "off",
                "acceptance": {"mode": "commands", "checks": [[sys.executable, "-m", "pytest", "-q"]]},
            },
            headers={"Idempotency-Key": "smoke"},
        )
        created.raise_for_status()
        run_id = created.json()["run_id"]
        service.run_once(run_id)

        run = client.get(f"/api/runs/{run_id}").json()
        artifacts = client.get(f"/api/runs/{run_id}/artifacts").json()
        patch = next((a for a in artifacts if a["summary"] == "delivery.patch"), None)
        patch_text = client.get(f"/api/runs/{run_id}/artifacts/{patch['artifact_id']}").text if patch else ""
        origin_untouched = "a - b" in (repo / "app.py").read_text(encoding="utf-8")

        verification = run.get("verification") or {}
        print(f"run_status:   {run['status']}")
        print(f"verification: {verification.get('status')}")
        print(f"artifacts:    {sorted(a['summary'] for a in artifacts)}")
        print(f"origin repo untouched: {origin_untouched}")
        print("delivery.patch:")
        print(patch_text)

        ok = (
            run["status"] == "succeeded"
            and verification.get("status") == "passed"
            and "+    return a + b" in patch_text
            and origin_untouched
        )
        print("SMOKE OK" if ok else "SMOKE FAILED")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
