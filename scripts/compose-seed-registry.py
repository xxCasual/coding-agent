#!/usr/bin/env python3
"""Seed workspace_registry.json with container-local paths for compose demo."""

from __future__ import annotations

import json
import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get("REVIEW_AGENT_DATA_ROOT", "/var/lib/review-agent")).expanduser()
REGISTRY_PATH = DATA_ROOT / "workspace_registry.json"

# In compose, repo is mounted at /app; override for host-side tests.
SAMPLE_ROOT = Path(
    os.environ.get("REVIEW_AGENT_COMPOSE_SAMPLE_ROOT", "/app/eval/samples")
).resolve()

DEMO_SPECS = [
    ("demo-fastapi", "Demo FastAPI service", "fastapi-service"),
    ("demo-python-backend", "Demo Python backend", "python-backend"),
    ("demo-llm-adapter", "Demo LLM adapter", "llm-adapter"),
]


def _demo_workspaces() -> list[dict[str, str]]:
    demos: list[dict[str, str]] = []
    for workspace_id, display_name, dirname in DEMO_SPECS:
        path = (SAMPLE_ROOT / dirname).resolve()
        if not path.is_dir():
            continue
        demos.append(
            {
                "id": workspace_id,
                "display_name": display_name,
                "path": str(path),
            }
        )
    return demos


def main() -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    existing: dict[str, dict[str, str]] = {}
    if REGISTRY_PATH.is_file():
        try:
            payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        for item in payload.get("workspaces") or []:
            workspace_id = str(item.get("id") or "")
            if workspace_id:
                existing[workspace_id] = {
                    "id": workspace_id,
                    "display_name": str(item.get("display_name") or workspace_id),
                    "path": str(item.get("path") or ""),
                }

    for demo in _demo_workspaces():
        existing[demo["id"]] = demo

    workspaces = [existing[key] for key in sorted(existing)]
    REGISTRY_PATH.write_text(
        json.dumps({"workspaces": workspaces}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"registry ready: {REGISTRY_PATH} ({len(workspaces)} workspace(s))")


if __name__ == "__main__":
    main()
