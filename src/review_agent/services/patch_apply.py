from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from review_agent.harness.workspace_revision import content_hash_bytes, file_content_hash

_DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
_PLUS_RE = re.compile(r"^\+\+\+ [ab]/(.+)$")
_MINUS_RE = re.compile(r"^--- [ab]/(.+)$")
_BEGIN_FILE_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+)$")


class PatchError(RuntimeError):
    """Unsafe or unsupported patch input."""


@dataclass(frozen=True)
class FilePatchPlan:
    path: str
    before_hash: str
    expected_after_hash: str | None
    is_new: bool
    is_delete: bool
    checkable: bool
    reason: str = ""


@dataclass(frozen=True)
class PatchPlan:
    patch_hash: str
    files: list[FilePatchPlan]
    patch_text: str


@dataclass(frozen=True)
class PatchApplyResult:
    success: bool
    already_applied: bool
    summary: str
    patch_hash: str
    changed_files: list[str] = field(default_factory=list)
    file_hashes: list[FilePatchPlan] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    error_code: str | None = None


def plan_patch(workspace: Path, patch: str) -> PatchPlan:
    root = Path(workspace).resolve()
    normalized = _normalize_patch(root, patch)
    targets = _parse_patch_targets(normalized)
    if not targets:
        raise PatchError("patch contains no file targets")
    files: list[FilePatchPlan] = []
    for target in targets:
        rel = str(target["path"])
        _assert_safe_relpath(root, rel)
        path = root / rel
        is_new = bool(target["is_new"])
        is_delete = bool(target["is_delete"])
        if path.is_symlink():
            resolved = path.resolve(strict=False)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise PatchError(f"symlink escapes workspace: {rel}") from exc
        before = file_content_hash(path if path.is_file() else None)
        # Full after-hash for arbitrary unified diffs requires applying in memory.
        # We support checkable text patches by simulating line edits when possible.
        expected, checkable, reason = _expected_after_hash(
            root, rel, normalized, is_new=is_new, is_delete=is_delete
        )
        files.append(
            FilePatchPlan(
                path=rel,
                before_hash=before,
                expected_after_hash=expected,
                is_new=is_new,
                is_delete=is_delete,
                checkable=checkable,
                reason=reason,
            )
        )
    patch_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    return PatchPlan(patch_hash=patch_hash, files=files, patch_text=normalized)


def apply_patch(workspace: Path, patch: str, *, force: bool = False) -> PatchApplyResult:
    root = Path(workspace).resolve()
    try:
        planned = plan_patch(root, patch)
    except PatchError as exc:
        return PatchApplyResult(
            success=False,
            already_applied=False,
            summary=str(exc),
            patch_hash=hashlib.sha256(patch.encode("utf-8")).hexdigest(),
            error_code="invalid_patch",
            stderr=str(exc),
        )

    uncheckable = [item for item in planned.files if not item.checkable]
    if uncheckable and not force:
        reasons = "; ".join(f"{item.path}: {item.reason or 'uncheckable'}" for item in uncheckable)
        return PatchApplyResult(
            success=False,
            already_applied=False,
            summary=f"patch requires manual check: {reasons}",
            patch_hash=planned.patch_hash,
            file_hashes=list(planned.files),
            error_code="patch_needs_attention",
        )

    if _already_applied(root, planned):
        return PatchApplyResult(
            success=True,
            already_applied=True,
            summary="patch already applied (after hashes match)",
            patch_hash=planned.patch_hash,
            changed_files=[item.path for item in planned.files],
            file_hashes=list(planned.files),
        )

    # Begin Patch locates old context uniquely above, but may omit surrounding
    # lines. Git otherwise rejects valid interior hunks without trailing context.
    apply_argv = ["git", "apply", "--whitespace=nowarn"]
    if _looks_like_begin_patch(patch):
        apply_argv.append("--unidiff-zero")
    completed = subprocess.run(
        apply_argv,
        cwd=root,
        input=planned.patch_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return PatchApplyResult(
            success=False,
            already_applied=False,
            summary="patch failed",
            patch_hash=planned.patch_hash,
            file_hashes=list(planned.files),
            stdout=completed.stdout,
            stderr=completed.stderr,
            error_code="patch_apply_failed",
        )

    mismatches = _verify_after_hashes(root, planned)
    if mismatches:
        return PatchApplyResult(
            success=False,
            already_applied=False,
            summary=f"patch applied but hash mismatch: {', '.join(mismatches)}",
            patch_hash=planned.patch_hash,
            changed_files=[item.path for item in planned.files],
            file_hashes=list(planned.files),
            stdout=completed.stdout,
            stderr=completed.stderr,
            error_code="patch_hash_mismatch",
        )

    return PatchApplyResult(
        success=True,
        already_applied=False,
        summary="patch applied",
        patch_hash=planned.patch_hash,
        changed_files=[item.path for item in planned.files],
        file_hashes=list(planned.files),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def reconcile_patch(workspace: Path, patch: str) -> PatchApplyResult:
    """Return already_applied without re-applying when after hashes match."""
    return apply_patch(workspace, patch)


def _already_applied(root: Path, planned: PatchPlan) -> bool:
    for item in planned.files:
        if not item.checkable or item.expected_after_hash is None:
            return False
        actual = file_content_hash((root / item.path) if not item.is_delete else None)
        if item.is_delete:
            if (root / item.path).exists():
                return False
        elif actual != item.expected_after_hash:
            return False
    return True


def _verify_after_hashes(root: Path, planned: PatchPlan) -> list[str]:
    mismatches: list[str] = []
    for item in planned.files:
        if not item.checkable or item.expected_after_hash is None:
            continue
        if item.is_delete:
            if (root / item.path).exists():
                mismatches.append(item.path)
            continue
        actual = file_content_hash(root / item.path)
        if actual != item.expected_after_hash:
            mismatches.append(item.path)
    return mismatches


def _assert_safe_relpath(root: Path, rel: str) -> None:
    if rel in {"/dev/null", "dev/null"} or rel.startswith("/"):
        raise PatchError(f"absolute or invalid path in patch: {rel}")
    if ".." in Path(rel).parts:
        raise PatchError(f"path traversal in patch: {rel}")
    candidate = (root / rel).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise PatchError(f"path escapes workspace: {rel}") from exc


def _looks_like_begin_patch(patch: str) -> bool:
    stripped = patch.lstrip()
    return stripped.startswith("*** Begin Patch") or _BEGIN_FILE_RE.match(stripped.splitlines()[0] if stripped else "") is not None


def _normalize_patch(root: Path, patch: str) -> str:
    if _looks_like_begin_patch(patch):
        return _begin_patch_to_unified(root, patch)
    return patch


def _begin_patch_to_unified(root: Path, patch: str) -> str:
    files = _parse_begin_patch_files(patch)
    if not files:
        raise PatchError("patch contains no file targets")
    chunks: list[str] = []
    for spec in files:
        rel = spec["path"]
        _assert_safe_relpath(root, rel)
        kind = spec["kind"]
        if kind == "add":
            chunks.append(_begin_add_to_unified(rel, spec["body_lines"]))
        elif kind == "delete":
            path = root / rel
            if not path.is_file():
                raise PatchError(f"delete target missing: {rel}")
            chunks.append(_begin_delete_to_unified(rel, path.read_text(encoding="utf-8")))
        else:
            path = root / rel
            if not path.is_file():
                raise PatchError(f"update target missing: {rel}")
            chunks.append(_begin_update_to_unified(path, rel, spec["hunks"]))
    text = "\n".join(chunks)
    if not text.endswith("\n"):
        text += "\n"
    return text


def _parse_begin_patch_files(patch: str) -> list[dict[str, object]]:
    files: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    hunk: list[str] | None = None
    for raw in patch.splitlines():
        if raw.strip() in {"*** Begin Patch", "*** End Patch"}:
            continue
        header = _BEGIN_FILE_RE.match(raw)
        if header:
            if current is not None:
                if hunk is not None:
                    current["hunks"].append(hunk)
                files.append(current)
            kind = header.group(1).lower()
            current = {"kind": kind, "path": header.group(2).strip(), "hunks": [], "body_lines": []}
            hunk = None
            continue
        if current is None:
            continue
        if current["kind"] == "add":
            if raw.startswith("+"):
                current["body_lines"].append(raw[1:])
            elif raw.startswith("-"):
                raise PatchError(f"add file {current['path']} contains deletion lines")
            else:
                current["body_lines"].append(raw[1:] if raw.startswith(" ") else raw)
            continue
        if raw.startswith("@@"):
            if hunk is not None:
                current["hunks"].append(hunk)
            hunk = []
            continue
        if hunk is None:
            hunk = []
        hunk.append(raw)
    if current is not None:
        if hunk is not None:
            current["hunks"].append(hunk)
        files.append(current)
    return files


def _hunk_old_new(hunk_lines: list[str]) -> tuple[list[str], list[str]]:
    old_lines: list[str] = []
    new_lines: list[str] = []
    for raw in hunk_lines:
        if raw.startswith("+"):
            new_lines.append(raw[1:])
        elif raw.startswith("-"):
            old_lines.append(raw[1:])
        else:
            text = raw[1:] if raw.startswith(" ") else raw
            old_lines.append(text)
            new_lines.append(text)
    return old_lines, new_lines


def _begin_update_to_unified(path: Path, rel: str, hunks: list[list[str]]) -> str:
    original = path.read_text(encoding="utf-8")
    original_lines = original.splitlines()
    located: list[tuple[int, list[str], list[str], list[str]]] = []
    for hunk in hunks:
        if not hunk:
            continue
        old_lines, new_lines = _hunk_old_new(hunk)
        if not old_lines:
            raise PatchError(f"empty old context for {rel}")
        start = _find_unique_block(original_lines, old_lines, rel)
        located.append((start, old_lines, new_lines, hunk))
    located.sort(key=lambda item: item[0])
    parts = [f"diff --git a/{rel} b/{rel}", f"--- a/{rel}", f"+++ b/{rel}"]
    line_shift = 0
    for start, old_lines, new_lines, hunk in located:
        old_start = start + 1
        new_start = old_start + line_shift
        parts.append(f"@@ -{old_start},{len(old_lines)} +{new_start},{len(new_lines)} @@")
        for raw in hunk:
            if raw.startswith("+") or raw.startswith("-") or raw.startswith(" "):
                parts.append(raw)
            else:
                parts.append(f" {raw}")
        line_shift += len(new_lines) - len(old_lines)
    return "\n".join(parts)


def _find_unique_block(original_lines: list[str], block: list[str], rel: str) -> int:
    matches = [
        index
        for index in range(0, len(original_lines) - len(block) + 1)
        if original_lines[index : index + len(block)] == block
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise PatchError(f"context not found in {rel}")
    raise PatchError(f"context matches {len(matches)} times in {rel}")


def _begin_add_to_unified(rel: str, body_lines: list[str]) -> str:
    count = len(body_lines)
    parts = [
        f"diff --git a/{rel} b/{rel}",
        "new file mode 100644",
        "--- /dev/null",
        f"+++ b/{rel}",
        f"@@ -0,0 +1,{count} @@" if count else "@@ -0,0 +0,0 @@",
    ]
    parts.extend(f"+{line}" for line in body_lines)
    return "\n".join(parts)


def _begin_delete_to_unified(rel: str, original: str) -> str:
    lines = original.splitlines()
    count = len(lines)
    parts = [
        f"diff --git a/{rel} b/{rel}",
        "deleted file mode 100644",
        f"--- a/{rel}",
        "+++ /dev/null",
        f"@@ -1,{count} +0,0 @@" if count else "@@ -0,0 +0,0 @@",
    ]
    parts.extend(f"-{line}" for line in lines)
    return "\n".join(parts)


def _parse_patch_targets(patch: str) -> list[dict[str, object]]:
    targets: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    for line in patch.splitlines():
        match = _DIFF_GIT_RE.match(line)
        if match:
            if current is not None:
                targets.append(current)
            path_b = match.group(2)
            current = {
                "path": path_b,
                "is_new": False,
                "is_delete": False,
            }
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current["is_new"] = True
        if line.startswith("deleted file mode"):
            current["is_delete"] = True
        plus = _PLUS_RE.match(line)
        if plus and plus.group(1) != "/dev/null":
            current["path"] = plus.group(1)
        minus = _MINUS_RE.match(line)
        if minus and minus.group(1) == "/dev/null":
            current["is_new"] = True
        if plus and plus.group(1) == "/dev/null":
            current["is_delete"] = True
    if current is not None:
        targets.append(current)
    # Fallback: +++ lines only
    if not targets:
        for line in patch.splitlines():
            plus = _PLUS_RE.match(line)
            if plus and plus.group(1) != "/dev/null":
                targets.append({"path": plus.group(1), "is_new": False, "is_delete": False})
    return targets


def _expected_after_hash(
    root: Path,
    rel: str,
    patch: str,
    *,
    is_new: bool,
    is_delete: bool,
) -> tuple[str | None, bool, str]:
    if is_delete:
        return "missing", True, ""
    # Extract the single-file hunk content from the patch for this path.
    body = _extract_new_file_body(patch, rel)
    if body is not None:
        return content_hash_bytes(body.encode("utf-8")), True, ""
    simulated = _simulate_unified_diff(root / rel, patch, rel, is_new=is_new)
    if simulated is None:
        return None, False, "cannot compute expected after hash for this patch shape"
    return content_hash_bytes(simulated.encode("utf-8")), True, ""


def _extract_new_file_body(patch: str, rel: str) -> str | None:
    lines = patch.splitlines()
    capturing = False
    body: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if capturing:
                break
            capturing = f" b/{rel}" in line or line.endswith(f" b/{rel}")
            body = []
            continue
        if not capturing:
            continue
        if line.startswith("+++ "):
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("@@"):
            continue
        if line.startswith("+"):
            body.append(line[1:])
        elif line.startswith("\\"):
            continue
        elif line.startswith("-"):
            return None
        elif line.startswith(" "):
            body.append(line[1:])
    if capturing and body:
        text = "\n".join(body)
        if not text.endswith("\n"):
            text += "\n"
        return text
    return None


def _simulate_unified_diff(path: Path, patch: str, rel: str, *, is_new: bool) -> str | None:
    if is_new and not path.exists():
        extracted = _extract_new_file_body(patch, rel)
        return extracted
    if not path.is_file():
        return None
    original = path.read_text(encoding="utf-8")
    original_lines = original.splitlines(keepends=True)
    hunks = _extract_hunks(patch, rel)
    if not hunks:
        return None
    # Apply hunks in reverse order of start line to keep indices stable.
    result = list(original_lines)
    for start, old_count, new_lines in sorted(hunks, key=lambda item: item[0], reverse=True):
        start_idx = max(start - 1, 0)
        end_idx = start_idx + old_count
        if end_idx > len(result) and old_count > 0:
            return None
        result[start_idx:end_idx] = new_lines
    return "".join(result)


def _extract_hunks(patch: str, rel: str) -> list[tuple[int, int, list[str]]]:
    lines = patch.splitlines(keepends=True)
    hunks: list[tuple[int, int, list[str]]] = []
    in_file = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            in_file = f" b/{rel}" in line or line.rstrip().endswith(f" b/{rel}")
            i += 1
            continue
        if not in_file:
            i += 1
            continue
        if line.startswith("@@"):
            header = line.strip()
            match = re.match(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", header)
            if not match:
                return []
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_lines: list[str] = []
            i += 1
            while i < len(lines):
                row = lines[i]
                if row.startswith("@@") or row.startswith("diff --git "):
                    break
                if row.startswith("+"):
                    new_lines.append(row[1:])
                elif row.startswith("-"):
                    pass
                elif row.startswith(" "):
                    new_lines.append(row[1:])
                elif row.startswith("\\"):
                    pass
                else:
                    break
                i += 1
            hunks.append((old_start, old_count, new_lines))
            continue
        i += 1
    return hunks
