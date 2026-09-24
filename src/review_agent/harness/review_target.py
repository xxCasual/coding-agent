"""Small, resolved review boundary; no task service or separate state machine."""
from __future__ import annotations

import difflib
import hashlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import field_validator, model_validator

from review_agent.harness.models import _FrozenModel
from review_agent.harness.workspace_revision import compute_workspace_revision, should_skip_relative

MAX_FILE_BYTES = 128_000
MAX_PATCH_CHARS = 48_000


class ReviewTarget(_FrozenModel):
    kind: Literal["run_changes", "workspace_changes", "paths"] = "run_changes"
    paths: list[str] = []
    focus: str = ""
    baseline: str = ""
    workspace_revision: str = ""

    @field_validator("paths")
    @classmethod
    def normalize_paths(cls, values: list[str]) -> list[str]:
        result = set()
        for value in values:
            path = PurePosixPath(value)
            if not value.strip() or path.is_absolute() or ".." in path.parts or "\\" in value:
                raise ValueError("review paths must be workspace-relative")
            result.add(path.as_posix())
        return sorted(result)

    @model_validator(mode="after")
    def require_paths(self):
        if self.kind == "paths" and not self.paths:
            raise ValueError("paths review requires a path scope")
        return self


def git_bytes(root: Path, *args: str, check: bool = True) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, timeout=15)
    if check and result.returncode:
        raise ValueError(f"Cannot read review Git baseline: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout if result.returncode == 0 else b""


def head_commit(root: Path) -> str:
    return git_bytes(root, "rev-parse", "--verify", "HEAD", check=False).decode().strip()


def source_paths(root: Path) -> list[str]:
    if (root / ".git").exists():
        raw = git_bytes(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return sorted(set(p for p in raw.decode().split("\0") if p))
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def in_scope(path: str, scopes: list[str]) -> bool:
    return not scopes or any(s == "." or path == s or path.startswith(s + "/") for s in scopes)


def source_signature(root: Path, *, exclude: list[str] | None = None) -> dict[str, str]:
    """Fingerprint only allowed snapshot content; never dereference an escaping link."""
    result = {}
    for rel in source_paths(root):
        if should_skip_relative(rel) or (exclude and in_scope(rel, exclude)):
            continue
        path = root / rel
        if not path.resolve().is_relative_to(root.resolve()):
            result[rel] = "escaping_symlink"
            continue
        if path.is_file():
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest() + f":{bool(path.stat().st_mode & 0o111)}"
    return result


def collect_patch(root: Path, *, baseline: str | None = None, paths: list[str] | None = None, exclude: list[str] | None = None,
                  baseline_root: Path | None = None) -> dict:
    """Review diff including untracked files, without staging or writing to the repo."""
    baseline_root = baseline_root or root
    baseline = head_commit(baseline_root) if baseline is None else baseline
    old_paths = set()
    if baseline:
        old_paths = set(git_bytes(baseline_root, "ls-tree", "-rz", "--name-only", baseline).decode().split("\0")) - {""}
    selected = sorted(p for p in old_paths | set(source_paths(root)) if in_scope(p, paths or []))
    chunks, unchecked, changed = [], [], []
    size = 0
    for rel in selected:
        if should_skip_relative(rel) or (exclude and in_scope(rel, exclude)):
            unchecked.append(f"{rel}: excluded from snapshot/review")
            continue
        if any(char in rel for char in "\n\r\t"):
            unchecked.append(f"{rel!r}: unsupported control character in path")
            continue
        path = root / rel
        if not path.resolve().is_relative_to(root.resolve()):
            unchecked.append(f"{rel}: escaping symlink not read")
            continue
        old = git_bytes(baseline_root, "show", f"{baseline}:{rel}") if rel in old_paths else None
        new = path.read_bytes() if path.is_file() else None
        if old == new:
            continue
        changed.append(rel)
        if max(len(old or b""), len(new or b"")) > MAX_FILE_BYTES:
            unchecked.append(f"{rel}: file exceeds {MAX_FILE_BYTES} bytes")
            continue
        try:
            before, after = (old or b"").decode("utf-8"), (new or b"").decode("utf-8")
            if "\0" in before + after:
                raise UnicodeError()
        except UnicodeError:
            unchecked.append(f"{rel}: binary/non-UTF-8 file")
            continue
        # Preserve line structure, including the conventional no-newline marker.
        body = list(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                    fromfile=f"a/{rel}" if old is not None else "/dev/null",
                    tofile=f"b/{rel}" if new is not None else "/dev/null"))
        text = f"diff --git a/{rel} b/{rel}\n"
        if old is None:
            text += "new file mode 100644\n"
        elif new is None:
            text += "deleted file mode 100644\n"
        text += "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in body)
        if size + len(text) > MAX_PATCH_CHARS:
            unchecked.append(f"{rel}: patch omitted by {MAX_PATCH_CHARS}-character limit")
            continue
        chunks.append(text)
        size += len(text)
    return {"baseline": baseline, "patch": "".join(chunks), "paths": changed, "unchecked": unchecked}


def resolve_target(root: Path, target: ReviewTarget) -> tuple[ReviewTarget, Path, dict]:
    root = root.resolve()
    if target.kind == "workspace_changes":
        meta = root.parent / "meta"
        manifest_path = meta / "workspace_review.json"
        if not manifest_path.is_file():
            raise ValueError("workspace_changes unavailable: input snapshot metadata is missing")
        material = json.loads(manifest_path.read_text())
        review_root = meta / "review_input"
        revision = compute_workspace_revision(review_root)
        if revision != material["workspace_revision"]:
            raise ValueError("workspace_changes input snapshot has changed")
        # Scope is applied to the saved original patch, never to the agent's newer source.
        if target.paths:
            sections = material["patch"].split("diff --git ")
            material["patch"] = "".join("diff --git " + s for s in sections[1:]
                if any(s.startswith(f"a/{p} b/{p}\n") for p in material["paths"] if in_scope(p, target.paths)))
            material["paths"] = [p for p in material["paths"] if in_scope(p, target.paths)]
    else:
        review_root = root
        revision = compute_workspace_revision(root)
        if target.kind == "paths":
            material = {"baseline": "", "patch": "", "paths": [p for p in source_paths(root) if in_scope(p, target.paths)], "unchecked": []}
            for scope in target.paths:
                if not any(in_scope(p, [scope]) for p in material["paths"]):
                    material["unchecked"].append(f"{scope}: path missing or outside snapshot")
        else:
            manifest = root.parent / "meta" / "input_manifest.json"
            baseline = json.loads(manifest.read_text())["baseline_commit"] if manifest.is_file() else head_commit(root)
            material = collect_patch(root, baseline=baseline, paths=target.paths)
    if target.baseline and target.baseline != material["baseline"]:
        raise ValueError("requested review baseline does not match input baseline")
    if target.workspace_revision and target.workspace_revision != revision:
        raise ValueError("requested review target revision is stale")
    resolved = target.model_copy(update={"baseline": material["baseline"], "workspace_revision": revision})
    return resolved, review_root, material


def review_cache_key(target: ReviewTarget, *, requirement: str, acceptance: str, verification: str,
                     evidence: list[str], profile_id: str, settings: dict) -> str:
    payload = {"version": 1, "target": target.model_dump(), "requirement": requirement,
               "acceptance": acceptance, "verification": verification, "evidence": evidence,
               "profile_id": profile_id, "settings": settings}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
