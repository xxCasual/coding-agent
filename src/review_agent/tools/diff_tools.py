import re
from pathlib import Path

from review_agent.models.diff import DiffHunk, DiffLine

HUNK_RE = re.compile(r"^@@ -(?P<old_start>\d+)(?:,(?P<old_len>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_len>\d+))? @@")


def detect_language_tool(file_path: str) -> str:
    return "python" if Path(file_path).suffix == ".py" else "unknown"


def _clean_path(path: str | None) -> str:
    if not path or path == "/dev/null":
        return ""
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def parse_diff_hunks_tool(raw_diff: str) -> list[DiffHunk]:
    hunks: list[DiffHunk] = []
    current_source = ""
    current_target = ""
    current_change = "modified"
    pending_rename_to = ""
    lines = raw_diff.splitlines()
    index = 0
    hunk_index = 0

    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            parts = line.split()
            current_source = _clean_path(parts[2]) if len(parts) > 2 else ""
            current_target = _clean_path(parts[3]) if len(parts) > 3 else current_source
            current_change = "modified"
            pending_rename_to = ""
            hunk_index = 0
            index += 1
            continue
        if line.startswith("new file mode"):
            current_change = "added"
        elif line.startswith("deleted file mode"):
            current_change = "deleted"
        elif line.startswith("rename to "):
            pending_rename_to = line.removeprefix("rename to ").strip()
            current_target = pending_rename_to
            current_change = "renamed"
        elif line.startswith("--- "):
            current_source = _clean_path(line.removeprefix("--- ").strip())
        elif line.startswith("+++ "):
            current_target = _clean_path(line.removeprefix("+++ ").strip())
            if current_source == "" and current_target:
                current_change = "added"
            elif current_target == "" and current_source:
                current_change = "deleted"
        elif line.startswith("@@ "):
            match = HUNK_RE.match(line)
            if not match:
                index += 1
                continue
            hunk_index += 1
            old_start = int(match.group("old_start"))
            old_len = int(match.group("old_len") or "1")
            new_start = int(match.group("new_start"))
            new_len = int(match.group("new_len") or "1")
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("diff --git "):
                next_line = lines[index]
                if next_line.startswith("@@ "):
                    break
                if next_line.startswith(("+", "-", " ")) or next_line == r"\ No newline at end of file":
                    body.append(next_line)
                index += 1
            parsed_lines = _parse_hunk_body(body, old_start, old_len, new_start)
            file_path = current_target or current_source or pending_rename_to
            hunks.append(
                DiffHunk(
                    hunk_id=f"{file_path}:{new_start}:{hunk_index}",
                    file_path=file_path,
                    change_type=current_change,
                    old_start=old_start if old_len else None,
                    old_end=old_start + old_len - 1 if old_len else None,
                    new_start=new_start if new_len else None,
                    new_end=new_start + new_len - 1 if new_len else None,
                    raw_diff="\n".join([line, *body]),
                    lines=parsed_lines,
                    language=detect_language_tool(file_path),
                )
            )
            continue
        index += 1
    return hunks


def _parse_hunk_body(body: list[str], old_start: int, old_len: int, new_start: int) -> list[DiffLine]:
    old_consuming = sum(1 for line in body if line.startswith((" ", "-")))
    missing_old = max(old_len - old_consuming, 0)
    old_line = old_start
    new_line = new_start
    parsed: list[DiffLine] = []
    old_consuming_seen = 0

    for raw in body:
        if raw == r"\ No newline at end of file":
            continue
        remaining_old_consuming = old_consuming - old_consuming_seen
        if missing_old and raw.startswith((" ", "-")) and remaining_old_consuming == 1:
            old_line += missing_old
            missing_old = 0

        if raw.startswith("+"):
            parsed.append(
                DiffLine(
                    old_line_no=None,
                    new_line_no=new_line,
                    content=raw[1:],
                    line_type="added",
                )
            )
            new_line += 1
        elif raw.startswith("-"):
            parsed.append(
                DiffLine(
                    old_line_no=old_line,
                    new_line_no=None,
                    content=raw[1:],
                    line_type="removed",
                )
            )
            old_line += 1
            old_consuming_seen += 1
        else:
            parsed.append(
                DiffLine(
                    old_line_no=old_line,
                    new_line_no=new_line,
                    content=raw[1:] if raw.startswith(" ") else raw,
                    line_type="context",
                )
            )
            old_line += 1
            new_line += 1
            old_consuming_seen += 1
    return parsed


def map_line_numbers_tool(hunk: DiffHunk) -> dict[str, dict[int, int | None]]:
    old_to_new: dict[int, int | None] = {}
    new_to_old: dict[int, int | None] = {}
    for line in hunk.lines:
        if line.line_type == "added" and line.new_line_no is not None:
            new_to_old[line.new_line_no] = None
        elif line.line_type == "removed" and line.old_line_no is not None:
            old_to_new[line.old_line_no] = None
        elif line.old_line_no is not None and line.new_line_no is not None:
            old_to_new[line.old_line_no] = line.new_line_no
            new_to_old[line.new_line_no] = line.old_line_no
    return {"old_to_new": old_to_new, "new_to_old": new_to_old}
