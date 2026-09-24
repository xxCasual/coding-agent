import hashlib
import json
import re
from typing import Any

from review_agent.models.diff import DiffHunk
from review_agent.models.finding import Finding


def start_line(hunk: DiffHunk) -> int:
    for line in hunk.lines:
        if line.line_type == "added" and line.new_line_no:
            return line.new_line_no
    return hunk.new_start or 1


def finding_id(hunk: DiffHunk, slug: str) -> str:
    digest = hashlib.sha1(f"{hunk.hunk_id}:{slug}".encode("utf-8")).hexdigest()[:8]
    return f"F-{digest}"


def make_finding(
    hunk: DiffHunk,
    slug: str,
    severity: str,
    category: str,
    title: str,
    evidence: str,
    explanation: str,
    suggestion: str,
    confidence: float,
    is_blocking: bool = False,
) -> Finding:
    line = start_line(hunk)
    return Finding(
        finding_id=finding_id(hunk, slug),
        hunk_id=hunk.hunk_id,
        file_path=hunk.file_path,
        start_line=line,
        end_line=line,
        severity=severity,
        category=category,
        title=title,
        evidence=evidence,
        explanation=explanation,
        suggestion=suggestion,
        confidence=confidence,
        is_blocking=is_blocking,
    )


def parse_json_object(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(content[start : end + 1])
        raise


def finding_from_payload(hunk: DiffHunk, item: dict[str, Any], index: int) -> Finding:
    try:
        return Finding.model_validate(item)
    except Exception:
        pass

    title = _text(item.get("title") or item.get("message") or item.get("issue"), "Potential issue")
    start = _int(item.get("start_line") or item.get("line"), start_line(hunk))
    end = _int(item.get("end_line"), start)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or f"llm-{index}"

    return Finding(
        finding_id=_text(item.get("finding_id"), finding_id(hunk, f"llm-{index}-{slug}")),
        hunk_id=_text(item.get("hunk_id"), hunk.hunk_id),
        file_path=_text(item.get("file_path") or item.get("file"), hunk.file_path),
        start_line=start,
        end_line=max(end, start),
        severity=_severity(item.get("severity")),
        category=_category(item.get("category")),
        title=title,
        evidence=_text(item.get("evidence") or item.get("message"), title),
        explanation=_text(
            item.get("explanation") or item.get("why") or item.get("impact"),
            "The reviewer identified this as an actionable risk in the changed code.",
        ),
        suggestion=_text(
            item.get("suggestion") or item.get("recommendation") or item.get("fix"),
            "Review the changed code and apply a focused fix.",
        ),
        confidence=_confidence(item.get("confidence")),
        is_blocking=bool(
            item.get("is_blocking")
            if item.get("is_blocking") is not None
            else item.get("blocking", False)
        ),
    )


def _text(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.7
    return min(max(confidence, 0.0), 1.0)


def _severity(value: Any) -> str:
    severity = str(value or "medium").lower()
    if severity in {"critical", "high", "medium", "low", "info"}:
        return severity
    return "medium"


def _category(value: Any) -> str:
    category = str(value or "bug").lower()
    valid_categories = {
        "bug",
        "security",
        "performance",
        "readability",
        "maintainability",
        "test",
        "style",
        "compatibility",
    }
    if category in valid_categories:
        return category
    return "bug"
