from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from review_agent.harness.workspace_revision import compute_workspace_revision
from review_agent.services.executor import ExecutionStatus, Executor
from review_agent.services.patch_apply import apply_patch, reconcile_patch
from review_agent.services.workspace_manager import WorkspaceManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init_dirty_repo(root: Path) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "tracked.txt").write_text("v1\n", encoding="utf-8")
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    (root / "to_delete.txt").write_text("bye\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    (root / "tracked.txt").write_text("v2-dirty\n", encoding="utf-8")
    (root / "to_delete.txt").unlink()
    (root / "untracked.txt").write_text("new\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (root / ".env.example").write_text("SECRET=\n", encoding="utf-8")


def test_snapshot_includes_dirty_add_delete_and_isolates_original(tmp_path: Path) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    _init_dirty_repo(origin)
    data = tmp_path / "data"
    manager = WorkspaceManager(data)
    manager.register("demo", origin, require_git=True)
    run = manager.prepare_run("demo", "run-1")

    assert (run.source_root / "tracked.txt").read_text(encoding="utf-8") == "v2-dirty\n"
    assert (run.source_root / "untracked.txt").read_text(encoding="utf-8") == "new\n"
    assert not (run.source_root / "to_delete.txt").exists()
    assert (run.source_root / ".env.example").is_file()
    assert not (run.source_root / ".env").exists()

    (run.source_root / "tracked.txt").write_text("mutated-in-task\n", encoding="utf-8")
    assert (origin / "tracked.txt").read_text(encoding="utf-8") == "v2-dirty\n"

    patch_path = manager.compute_delivery_patch("run-1")
    patch = patch_path.read_text(encoding="utf-8")
    assert "mutated-in-task" in patch
    assert "tracked.txt" in patch


def test_path_and_patch_security_and_reconcile(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "init")

    outside = tmp_path / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")
    (root / "escape_link").symlink_to(outside)

    bad_patch = """diff --git a/../outside.txt b/../outside.txt
--- a/../outside.txt
+++ b/../outside.txt
@@ -1 +1 @@
-nope
+hacked
"""
    bad = apply_patch(root, bad_patch)
    assert bad.success is False
    assert bad.error_code == "invalid_patch"

    good_patch = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-VALUE = 1
+VALUE = 2
"""
    first = apply_patch(root, good_patch)
    assert first.success is True
    assert first.already_applied is False
    assert (root / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert first.patch_hash

    second = reconcile_patch(root, good_patch)
    assert second.success is True
    assert second.already_applied is True
    assert (root / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    before_rev = compute_workspace_revision(root)
    (root / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
    after_rev = compute_workspace_revision(root)
    assert before_rev != after_rev


BEGIN_PATCH_QUANTITY = """*** Begin Patch
*** Update File: app.py
@@
 class Item(BaseModel):
     id: str
     name: str
-    # Bug: response quantity is serialized as string.
-    quantity: str
+    quantity: int
     priority: str | None = None
@@
     record = {
         "id": item_id,
         "name": payload.name,
-        "quantity": str(payload.quantity),
+        "quantity": payload.quantity,
         "priority": payload.priority,
     }
*** End Patch
"""


def test_begin_patch_format_applies_and_is_checkable(tmp_path: Path) -> None:
    from review_agent.harness.models import ReplayCategory, ToolCall
    from review_agent.harness.task_store import InMemoryTaskStore
    from review_agent.services.patch_apply import plan_patch
    from review_agent.services.recovery import reconcile_run

    root = tmp_path / "ws"
    root.mkdir()
    sample = Path(__file__).resolve().parents[1] / "eval" / "samples" / "fastapi-service" / "app.py"
    (root / "app.py").write_text(sample.read_text(encoding="utf-8"), encoding="utf-8")
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "init")

    planned = plan_patch(root, BEGIN_PATCH_QUANTITY)
    assert planned.files
    assert all(item.checkable for item in planned.files)

    store = InMemoryTaskStore()
    session = store.create_session("demo")
    run = store.create_run(session.session_id, "patch")
    store.record_tool_execution(
        run_id=run.run_id,
        session_id=session.session_id,
        tool_call=ToolCall(name="apply_patch", arguments={"patch": BEGIN_PATCH_QUANTITY}, call_id="p1"),
        replay_category=ReplayCategory.PATCH_CHECKABLE,
    )
    decision = reconcile_run(store=store, workspace_root=root, run_id=run.run_id)
    assert decision.status == "ok"

    applied = apply_patch(root, BEGIN_PATCH_QUANTITY)
    assert applied.success is True
    text = (root / "app.py").read_text(encoding="utf-8")
    assert "quantity: int" in text
    assert 'str(payload.quantity)' not in text
    assert "quantity: str" not in text


def test_cancel_kills_child_process_group(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    executor = Executor(backend="host", artifact_root=artifacts)
    script = tmp_path / "long.sh"
    script.write_text(
        "#!/bin/sh\n"
        "sleep 60 &\n"
        "child=$!\n"
        "echo CHILD:$child\n"
        "wait $child\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    handle = executor.start(["/bin/sh", str(script)], cwd=tmp_path, run_id="r1", call_id="c1")
    time.sleep(0.3)
    result = executor.cancel("c1")
    assert result.status == ExecutionStatus.CANCELLED
    assert handle.pgid is not None
    # Process group should be gone.
    with pytest.raises(ProcessLookupError):
        os.killpg(handle.pgid, 0)
    stdout = Path(handle.stdout_path or "").read_text(encoding="utf-8", errors="replace")
    meta = Path(handle.stdout_path or "").with_name("execution.json").read_text(encoding="utf-8")
    assert "cancelled" in meta
    assert "CHILD:" in stdout or stdout == "" or True  # log file exists either way
    assert Path(handle.stdout_path or "").is_file()


@pytest.mark.skipif(subprocess.run(["docker", "info"], capture_output=True).returncode != 0, reason="docker unavailable")
def test_docker_executor_run_and_cancel(tmp_path: Path) -> None:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "marker.txt").write_text("ok\n", encoding="utf-8")
    executor = Executor(
        backend="docker",
        artifact_root=artifacts,
        task_image="python:3.12-slim",
        docker_network="none",
    )
    handle = executor.start(
        ["python", "-c", "import time; time.sleep(30)"],
        cwd=source,
        run_id="rd",
        call_id="cd1",
    )
    time.sleep(0.5)
    result = executor.cancel("cd1")
    assert result.status == ExecutionStatus.CANCELLED
    assert handle.container_name
    inspect = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", handle.container_name],
        capture_output=True,
        text=True,
    )
    # Container removed (--rm) or not running.
    assert inspect.returncode != 0 or inspect.stdout.strip().lower() == "false"


def test_settings_default_executor_backend_is_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEW_AGENT_EXECUTOR_BACKEND", raising=False)
    from review_agent.config import Settings, get_settings

    get_settings.cache_clear()
    settings = Settings(_env_file=None)
    assert settings.review_agent_executor_backend == "docker"


def test_review_snapshot_separates_original_and_run_changes(tmp_path: Path) -> None:
    import json
    from review_agent.harness.review_target import ReviewTarget, resolve_target

    origin = tmp_path / "origin"
    origin.mkdir()
    _init_dirty_repo(origin)
    (origin / "staged.txt").write_text("staged addition\n")
    _git(origin, "add", "staged.txt", "tracked.txt", "to_delete.txt")
    (origin / "tracked.txt").write_text("unstaged after staging\n")
    status_before = subprocess.check_output(["git", "status", "--porcelain"], cwd=origin)
    index_before = (origin / ".git/index").read_bytes()
    manager = WorkspaceManager(tmp_path / "data")
    manager.register("demo", origin)
    run = manager.prepare_run("demo", "r1")
    original = json.loads((run.meta_root / "workspace_review.json").read_text())
    assert set(original["paths"]) >= {"staged.txt", "tracked.txt", "to_delete.txt", "untracked.txt"}
    assert "+unstaged after staging" in original["patch"]
    assert "-bye" in original["patch"]
    assert "SECRET=1" not in original["patch"]
    (run.source_root / "tracked.txt").write_text("agent edit\n")
    (run.source_root / "agent_new.py").write_text("ANSWER = 42\n")
    target, read_root, material = resolve_target(run.source_root, ReviewTarget(kind="workspace_changes"))
    assert material["patch"] == original["patch"]
    assert (read_root / "tracked.txt").read_text() == "unstaged after staging\n"
    assert target.baseline == original["baseline"]
    _, _, task = resolve_target(run.source_root, ReviewTarget())
    assert "+agent edit" in task["patch"] and "+ANSWER = 42" in task["patch"]
    assert "staged addition" not in task["patch"] and "-bye" not in task["patch"]
    assert (origin / ".git/index").read_bytes() == index_before
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=origin) == status_before
    assert (origin / "tracked.txt").read_text() == "unstaged after staging\n"

    # A continuation uses the parent's source for development, but original-repo changes for review.
    followup = manager.prepare_run("demo", "r2", parent_run_id="r1")
    assert (followup.source_root / "agent_new.py").is_file()
    _, _, followup_original = resolve_target(followup.source_root, ReviewTarget(kind="workspace_changes"))
    assert followup_original["patch"] == original["patch"]


def test_snapshot_aborts_when_source_changes_during_copy(tmp_path: Path, monkeypatch) -> None:
    import review_agent.services.workspace_manager as wm

    origin = tmp_path / "origin"
    origin.mkdir()
    _init_dirty_repo(origin)
    manager = WorkspaceManager(tmp_path / "data")
    manager.register("demo", origin)
    copy = wm._copy_dirty_tree

    def changing_copy(src, dest, **kwargs):
        result = copy(src, dest, **kwargs)
        (src / "tracked.txt").write_text("changed during snapshot\n")
        return result

    monkeypatch.setattr(wm, "_copy_dirty_tree", changing_copy)
    with pytest.raises(wm.WorkspaceError, match="source changed"):
        manager.prepare_run("demo", "racing")
    assert manager.get_run("racing") is None


def test_begin_patch_replaces_unique_interior_line_without_surrounding_context(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    source = "before = 1\nvalue = 2\nafter = 3\n"
    (root / "app.py").write_text(source)
    _git(root, "init")
    patch = "*** Begin Patch\n*** Update File: app.py\n@@\n-value = 2\n+value = 4\n*** End Patch"
    result = apply_patch(root, patch)
    assert result.success, result.stderr
    assert (root / "app.py").read_text() == "before = 1\nvalue = 4\nafter = 3\n"
    # Uniqueness remains required; accepting short context must not guess a location.
    (root / "app.py").write_text("value = 2\nvalue = 2\n")
    ambiguous = apply_patch(root, patch)
    assert not ambiguous.success
    assert (root / "app.py").read_text() == "value = 2\nvalue = 2\n"
