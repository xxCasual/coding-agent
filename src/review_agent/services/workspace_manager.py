from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from review_agent.harness.review_target import collect_patch, source_signature, in_scope
from review_agent.harness.workspace_revision import (
    compute_workspace_revision,
    SKIP_DIR_NAMES,
    SECRET_FILE_NAMES,
    should_skip_relative,
)

REGISTRY_FILENAME = "workspace_registry.json"


@dataclass(frozen=True)
class RegisteredWorkspace:
    workspace_id: str
    path: Path
    display_name: str


class WorkspaceError(RuntimeError):
    """Raised when a registered workspace cannot be snapshotted safely."""


@dataclass(frozen=True)
class RunWorkspace:
    workspace_id: str
    run_id: str
    source_root: Path
    meta_root: Path
    artifacts_root: Path
    memory_root: Path
    baseline_commit: str
    manifest_path: Path
    registered_path: Path


@dataclass
class WorkspaceManager:
    """Independent dirty-tree snapshots and stable per-run directories."""

    data_root: Path
    _registry: dict[str, Path] = field(default_factory=dict, init=False)
    _names: dict[str, str] = field(default_factory=dict, init=False)
    _runs: dict[str, RunWorkspace] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.data_root = Path(self.data_root).expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)
        self._load_registry()

    def _registry_path(self) -> Path:
        return self.data_root / REGISTRY_FILENAME

    def _load_registry(self) -> None:
        path = self._registry_path()
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for item in payload.get("workspaces") or []:
            workspace_id = str(item.get("id") or "")
            raw_path = item.get("path")
            if not workspace_id or not raw_path:
                continue
            self._registry[workspace_id] = Path(raw_path).expanduser()
            self._names[workspace_id] = str(item.get("display_name") or workspace_id)

    def _save_registry(self) -> None:
        items = []
        for workspace_id, path in sorted(self._registry.items()):
            items.append(
                {
                    "id": workspace_id,
                    "display_name": self._names.get(workspace_id) or workspace_id,
                    "path": str(path),
                }
            )
        self._registry_path().write_text(
            json.dumps({"workspaces": items}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def register(
        self,
        workspace_id: str,
        path: str | Path,
        *,
        require_git: bool = False,
        display_name: str | None = None,
    ) -> Path:
        root = Path(path).resolve()
        if not root.is_dir():
            raise WorkspaceError(f"workspace path is not a directory: {root}")
        if require_git and not (root / ".git").exists():
            raise WorkspaceError(f"workspace must be a Git repository: {root}")
        self._registry[workspace_id] = root
        self._names[workspace_id] = display_name or self._names.get(workspace_id) or workspace_id
        (self.data_root / "workspaces" / workspace_id).mkdir(parents=True, exist_ok=True)
        self._save_registry()
        return root

    def list_workspaces(self) -> list[RegisteredWorkspace]:
        self._load_registry()
        result: list[RegisteredWorkspace] = []
        for workspace_id, path in sorted(self._registry.items()):
            result.append(
                RegisteredWorkspace(
                    workspace_id=workspace_id,
                    path=path,
                    display_name=self._names.get(workspace_id) or workspace_id,
                )
            )
        return result

    def get_registered(self, workspace_id: str) -> RegisteredWorkspace | None:
        self._load_registry()
        path = self._registry.get(workspace_id)
        if path is None:
            return None
        return RegisteredWorkspace(
            workspace_id=workspace_id,
            path=path,
            display_name=self._names.get(workspace_id) or workspace_id,
        )

    def memory_root_for(self, workspace_id: str) -> Path:
        path = self.data_root / "workspaces" / workspace_id / "memory"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def prepare_run(
        self,
        workspace_id: str,
        run_id: str,
        *,
        parent_run_id: str | None = None,
    ) -> RunWorkspace:
        if workspace_id not in self._registry:
            self._load_registry()
        if workspace_id not in self._registry and parent_run_id is None:
            raise WorkspaceError(f"unknown workspace_id: {workspace_id}")
        base = self.data_root / "workspaces" / workspace_id / "runs" / run_id
        source = base / "source"
        meta = base / "meta"
        artifacts = base / "artifacts"
        for path in (source, meta, artifacts):
            if path.exists():
                shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)

        registered = self._registry.get(workspace_id)
        if parent_run_id:
            parent = self._runs.get(parent_run_id) or self._load_run_meta(workspace_id, parent_run_id)
            origin = parent.source_root
            origin_label = f"parent_run:{parent_run_id}"
            origin_head, origin_dirty = _git_head_and_dirty(origin)
        else:
            assert registered is not None
            origin = registered
            origin_label = str(origin)
            origin_head, origin_dirty = _git_head_and_dirty(origin)

        exclude = [self.data_root.relative_to(origin).as_posix()] if self.data_root.is_relative_to(origin) else []
        before = source_signature(origin, exclude=exclude)
        copied, excluded, rejected = _copy_dirty_tree(origin, source, exclude=exclude)
        if rejected:
            raise WorkspaceError("; ".join(rejected))

        if before != source_signature(origin, exclude=exclude) or before != source_signature(source) or origin_head != _git_head_and_dirty(origin)[0]:
            raise WorkspaceError("source changed while preparing input snapshot; retry preparation")

        # Keep the registered repository's original diff and matching read-only content
        # outside task source. Follow-up runs still refer to the registered repository.
        review_origin = registered or origin
        review_exclude = [self.data_root.relative_to(review_origin).as_posix()] if self.data_root.is_relative_to(review_origin) else []
        review_before = source_signature(review_origin, exclude=review_exclude)
        review_head = _git_head_and_dirty(review_origin)[0]
        review_source = meta / "review_input"
        review_source.mkdir()
        _, review_excluded, review_rejected = _copy_dirty_tree(review_origin, review_source, exclude=review_exclude)
        if review_rejected:
            raise WorkspaceError("; ".join(review_rejected))
        material = collect_patch(review_source, baseline=review_head or "",
                                 baseline_root=review_origin, exclude=review_exclude)
        if (review_before != source_signature(review_origin, exclude=review_exclude)
                or review_before != source_signature(review_source)
                or review_head != _git_head_and_dirty(review_origin)[0]
                or (review_origin == origin and review_before != before)):
            raise WorkspaceError("source changed while collecting original review diff; retry preparation")
        material.update({"workspace_revision": compute_workspace_revision(review_source),
                         "origin": str(review_origin), "excluded": review_excluded})
        (meta / "workspace_review.json").write_text(json.dumps(material, ensure_ascii=False, indent=2) + "\n")

        baseline = _init_independent_baseline(source)
        manifest = {
            "workspace_id": workspace_id,
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "origin": origin_label,
            "registered_path": str(registered) if registered else None,
            "origin_head": origin_head,
            "origin_dirty": origin_dirty,
            "baseline_commit": baseline,
            "copied_files": copied,
            "excluded": excluded,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        manifest_path = meta / "input_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        run = RunWorkspace(
            workspace_id=workspace_id,
            run_id=run_id,
            source_root=source.resolve(),
            meta_root=meta.resolve(),
            artifacts_root=artifacts.resolve(),
            memory_root=self.memory_root_for(workspace_id),
            baseline_commit=baseline,
            manifest_path=manifest_path,
            registered_path=(registered or origin).resolve(),
        )
        self._runs[run_id] = run
        (meta / "run_workspace.json").write_text(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "run_id": run_id,
                    "source_root": str(run.source_root),
                    "meta_root": str(run.meta_root),
                    "artifacts_root": str(run.artifacts_root),
                    "memory_root": str(run.memory_root),
                    "baseline_commit": baseline,
                    "registered_path": str(run.registered_path),
                    "manifest_path": str(manifest_path),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return run

    def get_run(self, run_id: str) -> RunWorkspace | None:
        if run_id in self._runs:
            return self._runs[run_id]
        # Best-effort reload from data root.
        for workspace_dir in (self.data_root / "workspaces").glob("*"):
            meta = workspace_dir / "runs" / run_id / "meta" / "run_workspace.json"
            if meta.is_file():
                return self._load_run_meta(workspace_dir.name, run_id)
        return None

    def compute_delivery_patch(self, run_id: str) -> Path:
        run = self.get_run(run_id)
        if run is None:
            raise WorkspaceError(f"unknown run_id: {run_id}")
        # Stage all changes including untracked so the patch is complete.
        _run_git(["add", "-A"], cwd=run.source_root)
        result = _run_git(
            ["diff", "--binary", "--cached", run.baseline_commit],
            cwd=run.source_root,
            check=False,
        )
        patch_path = run.artifacts_root / "delivery.patch"
        patch_path.write_text(result.stdout, encoding="utf-8")
        # Leave the index staged for inspection; do not commit delivery.
        return patch_path

    def _load_run_meta(self, workspace_id: str, run_id: str) -> RunWorkspace:
        meta_file = (
            self.data_root / "workspaces" / workspace_id / "runs" / run_id / "meta" / "run_workspace.json"
        )
        if not meta_file.is_file():
            raise WorkspaceError(f"missing run metadata for {run_id}")
        payload = json.loads(meta_file.read_text(encoding="utf-8"))
        run = RunWorkspace(
            workspace_id=payload["workspace_id"],
            run_id=payload["run_id"],
            source_root=Path(payload["source_root"]),
            meta_root=Path(payload["meta_root"]),
            artifacts_root=Path(payload["artifacts_root"]),
            memory_root=Path(payload["memory_root"]),
            baseline_commit=payload["baseline_commit"],
            manifest_path=Path(payload["manifest_path"]),
            registered_path=Path(payload["registered_path"]),
        )
        self._runs[run_id] = run
        if workspace_id not in self._registry:
            self._registry[workspace_id] = run.registered_path
            self._names.setdefault(workspace_id, workspace_id)
        return run


def _copy_dirty_tree(origin: Path, dest: Path, *, exclude: list[str] | None = None) -> tuple[list[str], list[str], list[str]]:
    copied: list[str] = []
    excluded: list[str] = []
    rejected: list[str] = []
    files = _list_snapshot_files(origin)
    for rel in files:
        if should_skip_relative(rel) or (exclude and in_scope(rel, exclude)):
            excluded.append(rel)
            continue
        src = origin / rel
        if src.is_symlink():
            target = src.resolve(strict=False)
            try:
                target.relative_to(origin.resolve())
            except ValueError:
                rejected.append(f"symlink escapes workspace: {rel} -> {target}")
                continue
            if not target.exists():
                rejected.append(f"broken symlink: {rel}")
                continue
        if not src.exists():
            # Tracked deletion: omit from copy (desired).
            excluded.append(f"deleted:{rel}")
            continue
        if src.is_dir():
            continue
        if src.name in SECRET_FILE_NAMES:
            excluded.append(rel)
            continue
        # Skip nested .git and caches already handled by should_skip_relative.
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            # Materialize as regular file/dir content, not an escaping link.
            if src.is_file():
                shutil.copy2(src, dst, follow_symlinks=True)
            else:
                rejected.append(f"unsupported symlink target type: {rel}")
                continue
        else:
            shutil.copy2(src, dst)
        copied.append(rel)
    return copied, excluded, rejected


def _list_snapshot_files(origin: Path) -> list[str]:
    """Tracked current paths + non-ignored untracked; deletions omitted from disk list."""
    paths: set[str] = set()
    if (origin / ".git").exists():
        tracked = _run_git(["ls-files", "-z"], cwd=origin, check=False)
        untracked = _run_git(["ls-files", "-z", "-o", "--exclude-standard"], cwd=origin, check=False)
        for blob in (tracked.stdout, untracked.stdout):
            for item in blob.split("\0"):
                if item:
                    paths.add(item)
        # Deleted tracked files are absent from disk; omit them from the copy set.
    else:
        for path in origin.rglob("*"):
            if not path.is_file() and not path.is_symlink():
                continue
            rel = path.relative_to(origin).as_posix()
            if any(part in SKIP_DIR_NAMES for part in Path(rel).parts):
                continue
            paths.add(rel)
    return sorted(paths)


def _init_independent_baseline(source: Path) -> str:
    # Empty template avoids copying hook samples (also helps restricted sandboxes).
    _run_git(["-c", "init.templateDir=", "init"], cwd=source)
    _run_git(["config", "user.email", "review-agent@localhost"], cwd=source)
    _run_git(["config", "user.name", "review-agent"], cwd=source)
    _run_git(["add", "--force", "-A"], cwd=source)
    # Allow empty baseline (empty repo).
    commit = _run_git(
        ["commit", "--allow-empty", "-m", "input baseline"],
        cwd=source,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "1970-01-01T00:00:00Z",
            "GIT_COMMITTER_DATE": "1970-01-01T00:00:00Z",
        },
    )
    head = _run_git(["rev-parse", "HEAD"], cwd=source)
    return head.stdout.strip() or commit.stdout.strip()


def _git_head_and_dirty(root: Path) -> tuple[str | None, bool]:
    if not (root / ".git").exists():
        return None, True
    head = _run_git(["rev-parse", "HEAD"], cwd=root, check=False)
    status = _run_git(["status", "--porcelain"], cwd=root, check=False)
    head_value = head.stdout.strip() if head.returncode == 0 else None
    dirty = bool(status.stdout.strip())
    return head_value, dirty


def _run_git(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if check and completed.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed in {cwd}: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    return completed
