from __future__ import annotations

from typing import Any, Literal

ReviewerMode = Literal["default", "off"]


def normalize_reviewer_mode(value: str | None) -> ReviewerMode:
    if (value or "default").strip().lower() == "off":
        return "off"
    return "default"


def initial_review_state(mode: str | None = "default") -> dict[str, Any]:
    normalized = normalize_reviewer_mode(mode)
    return {
        "enabled": normalized != "off",
        "mode": normalized,
        "status": None,
        "subtask_id": None,
        "workspace_revision": None,
        "findings": [],
        "coverage": [],
        "unchecked": [],
        "warnings": [],
        "usage": None,
        "dispositions": [],
    }
