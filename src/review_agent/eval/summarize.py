from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


def summarize_jsonl(path: Path) -> dict[str, Any]:
    """Aggregate known eval JSONL rows. Does not reimplement scoring."""
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    total = len(rows)
    successes = sum(1 for row in rows if row.get("outcome") == "success")
    reasons = Counter(row.get("outcome") for row in rows)
    variants = {}
    for row in rows:
        variant = row.get("variant") or "unknown"
        bucket = variants.setdefault(variant, {"total": 0, "success": 0})
        bucket["total"] += 1
        if row.get("outcome") == "success":
            bucket["success"] += 1
    return {
        "attempts": total,
        "successes": successes,
        "completion_rate": (successes / total) if total else None,
        "outcomes": dict(reasons),
        "by_variant": variants,
    }
