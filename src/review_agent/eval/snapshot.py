from __future__ import annotations

import json
import shutil
from pathlib import Path

from review_agent.harness.workspace_revision import compute_workspace_revision
from review_agent.services.workspace_manager import WorkspaceError, _init_independent_baseline


def materialize_sample(sample_root: Path, dest: Path) -> str:
    """Copy a frozen sample tree into an isolated directory and return its tree hash.

    Git baseline for the Agent run is created later by WorkspaceManager on the run snapshot.
    """
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(sample_root, dest)
    try:
        _init_independent_baseline(dest)
    except WorkspaceError:
        # Nested .git may be blocked in restricted sandboxes; run snapshots still baseline.
        pass
    return compute_workspace_revision(dest)


def load_manifest(path: Path, *, repo_root: Path) -> list:
    from review_agent.eval.models import EvalTask

    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    return [EvalTask.from_dict(item, repo_root=repo_root) for item in tasks]
