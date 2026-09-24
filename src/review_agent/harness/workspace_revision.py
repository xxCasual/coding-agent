from __future__ import annotations

import hashlib
from pathlib import Path

# Shared with WorkspaceManager / Executor exclusions.
SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".memory",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        "node_modules",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".review-agent",
    }
)

SECRET_FILE_NAMES = frozenset({".env", ".env.local", ".env.production"})


def compute_workspace_revision(workspace_root: str | Path) -> str:
    """Content digest of task source files (excludes caches/env/metadata)."""
    root = Path(workspace_root).resolve()
    digest = hashlib.sha256()
    for path in _iter_source_files(root):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:24]


def file_content_hash(path: Path | None, *, missing_token: str = "missing") -> str:
    if path is None or not path.is_file():
        return missing_token
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def should_skip_relative(rel: str) -> bool:
    parts = Path(rel).parts
    if any(part in SKIP_DIR_NAMES for part in parts):
        return True
    name = Path(rel).name
    if name in SECRET_FILE_NAMES:
        return True
    return False


def _iter_source_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if should_skip_relative(rel):
            continue
        yield path
