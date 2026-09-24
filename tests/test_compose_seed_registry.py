"""Deterministic checks for compose workspace registry seeding."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_compose_seed_registry_writes_container_paths(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    data_root = tmp_path / "data"
    env = {
        **os.environ,
        "REVIEW_AGENT_DATA_ROOT": str(data_root),
        "REVIEW_AGENT_COMPOSE_SAMPLE_ROOT": str(repo_root / "eval" / "samples"),
        "PYTHONPATH": str(repo_root / "src"),
    }
    script = Path(__file__).resolve().parents[1] / "scripts" / "compose-seed-registry.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "registry ready" in result.stdout

    registry_path = data_root / "workspace_registry.json"
    assert registry_path.is_file()
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    by_id = {item["id"]: item["path"] for item in payload.get("workspaces", [])}
    assert "demo-fastapi" in by_id
    assert by_id["demo-fastapi"].endswith("eval/samples/fastapi-service")
