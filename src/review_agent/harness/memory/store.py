from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from review_agent.harness.memory.models import MemoryEntry, MemoryIndexItem, MemoryType

INDEX_FILENAME = "MEMORY.md"
LOCK_FILENAME = ".lock"


class MemoryStore:
    def __init__(
        self,
        workspace_root: str | Path,
        memory_dir: str | Path = ".memory",
        *,
        memory_root: str | Path | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        # memory_root may point at a trusted data dir outside the writable workspace (M04).
        self.memory_dir = self._resolve_memory_dir(memory_root if memory_root is not None else memory_dir)
        self.index_path = self.memory_dir / INDEX_FILENAME
        self.lock_path = self.memory_dir / LOCK_FILENAME

    def ensure_initialized(self) -> None:
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self.index_path.write_text(_render_index([]), encoding="utf-8")

    def index_markdown(self) -> str:
        self.ensure_initialized()
        return self.index_path.read_text(encoding="utf-8")

    def index_items(self) -> list[MemoryIndexItem]:
        return [
            MemoryIndexItem(
                id=entry.id,
                name=entry.name,
                description=entry.description,
                type=entry.type,
                path=entry.path or "",
                updated_at=entry.updated_at,
                tags=tuple(entry.tags),
            )
            for entry in self.list_entries()
        ]

    def list_entries(self) -> list[MemoryEntry]:
        self.ensure_initialized()
        entries: list[MemoryEntry] = []
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name == INDEX_FILENAME:
                continue
            entries.append(_parse_entry(path, self.memory_dir))
        return sorted(entries, key=lambda entry: entry.updated_at, reverse=True)

    def get_entry(self, memory_id: str) -> MemoryEntry | None:
        for entry in self.list_entries():
            if entry.id == memory_id:
                return entry
        return None

    def write_entry(self, entry: MemoryEntry) -> Path:
        self.ensure_initialized()
        now = _now()
        if not entry.created_at:
            entry.created_at = now
        entry.updated_at = now
        existing_path = self._path_for_existing_entry(entry.id)
        path = existing_path or self.memory_dir / f"{entry.id}-{_slugify(entry.name)}.md"
        entry.path = _display_path(path, self.workspace_root)
        path.write_text(_render_entry(entry), encoding="utf-8")
        self.rebuild_index()
        return path

    def write_if_new(self, entry: MemoryEntry) -> MemoryEntry | None:
        if self.find_duplicate(entry) is not None:
            return None
        self.write_entry(entry)
        return entry

    def delete_entry(self, entry: MemoryEntry) -> None:
        if not entry.path:
            return
        path = self.workspace_root / entry.path
        if path.exists() and self._is_inside_workspace(path):
            path.unlink()

    def find_duplicate(self, candidate: MemoryEntry) -> MemoryEntry | None:
        candidate_fingerprint = _fingerprint(candidate)
        for entry in self.list_entries():
            if _fingerprint(entry) == candidate_fingerprint:
                return entry
        return None

    def rebuild_index(self) -> None:
        self.ensure_initialized()
        self.index_path.write_text(_render_index(self.list_entries()), encoding="utf-8")

    def count_memory_files(self) -> int:
        self.ensure_initialized()
        return sum(1 for path in self.memory_dir.glob("*.md") if path.name != INDEX_FILENAME)

    def _path_for_existing_entry(self, memory_id: str) -> Path | None:
        for path in self.memory_dir.glob(f"{memory_id}-*.md"):
            return path
        return None

    def _resolve_memory_dir(self, memory_dir: str | Path) -> Path:
        candidate = Path(memory_dir)
        if not candidate.is_absolute():
            candidate = self.workspace_root / candidate
            resolved = candidate.resolve(strict=False)
            if not _is_relative_to(resolved, self.workspace_root):
                raise ValueError("memory_dir must stay inside the workspace")
            return resolved
        # Absolute memory_root is allowed for injected trusted directories.
        return candidate.resolve(strict=False)

    def _is_inside_workspace(self, path: Path) -> bool:
        return _is_relative_to(path.resolve(strict=False), self.workspace_root)


def _parse_entry(path: Path, memory_dir: Path) -> MemoryEntry:
    from review_agent.harness.memory.models import MemoryScope

    metadata, content = _split_frontmatter(path.read_text(encoding="utf-8"))
    memory_type = MemoryType(metadata.get("type", MemoryType.PROJECT.value))
    scope_raw = metadata.get("scope", MemoryScope.WORKSPACE.value)
    try:
        scope = MemoryScope(scope_raw)
    except ValueError:
        scope = MemoryScope.WORKSPACE
    related = [part.strip() for part in metadata.get("related_file_hashes", "").split(",") if part.strip()]
    evidence = [part.strip() for part in metadata.get("evidence_refs", "").split(",") if part.strip()]
    user_confirmed = metadata.get("user_confirmed", "false").lower() in {"1", "true", "yes"}
    entry = MemoryEntry(
        id=metadata.get("id", path.stem),
        name=metadata.get("name", path.stem),
        description=metadata.get("description", ""),
        type=memory_type,
        tags=_parse_tags(metadata.get("tags", "")),
        created_at=metadata.get("created_at", ""),
        updated_at=metadata.get("updated_at", ""),
        source=metadata.get("source", "agent"),
        content=content.strip(),
        path=f".memory/{path.relative_to(memory_dir).as_posix()}"
        if _is_relative_to(path, memory_dir)
        else path.as_posix(),
        scope=scope,
        related_file_hashes=related,
        evidence_refs=evidence,
        user_confirmed=user_confirmed,
        verification_status=metadata.get("verification_status", "unverified"),
    )
    return entry


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---\n"):
        return {}, text
    if "\n---\n" not in text[len("---\n") :]:
        return {}, text
    _, rest = text.split("---\n", 1)
    raw_metadata, content = rest.split("\n---\n", 1)
    metadata: dict[str, str] = {}
    for line in raw_metadata.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, content


def _render_entry(entry: MemoryEntry) -> str:
    tags = ", ".join(sorted(set(entry.tags)))
    metadata = {
        "id": entry.id,
        "name": entry.name,
        "description": entry.description,
        "type": entry.type.value,
        "tags": tags,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "source": entry.source,
        "scope": getattr(entry.scope, "value", entry.scope),
        "related_file_hashes": ", ".join(entry.related_file_hashes),
        "evidence_refs": ", ".join(entry.evidence_refs),
        "user_confirmed": "true" if entry.user_confirmed else "false",
        "verification_status": entry.verification_status or "unverified",
    }
    frontmatter = "\n".join(f"{key}: {_clean_meta_value(value)}" for key, value in metadata.items())
    return f"---\n{frontmatter}\n---\n\n{entry.content.strip()}\n"


def _render_index(entries: list[MemoryEntry]) -> str:
    lines = [
        "# Memory Index",
        "",
        "| id | type | name | description | path | updated_at |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for entry in sorted(entries, key=lambda item: item.updated_at, reverse=True):
        lines.append(
            "| "
            + " | ".join(
                [
                    _escape_table(entry.id),
                    _escape_table(entry.type.value),
                    _escape_table(entry.name),
                    _escape_table(entry.description),
                    _escape_table(entry.path or ""),
                    _escape_table(entry.updated_at),
                ]
            )
            + " |"
        )
    if not entries:
        lines.append("| | | | | | |")
    return "\n".join(lines) + "\n"


def _parse_tags(value: str) -> list[str]:
    return [tag.strip() for tag in value.split(",") if tag.strip()]


def _clean_meta_value(value: str) -> str:
    return str(value).replace("\n", " ").strip()


def _escape_table(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _display_path(path: Path, workspace_root: Path) -> str:
    if _is_relative_to(path, workspace_root):
        return path.relative_to(workspace_root).as_posix()
    return path.as_posix()


def _fingerprint(entry: MemoryEntry) -> str:
    text = f"{entry.type.value}:{entry.name}:{entry.description}:{entry.content}"
    return re.sub(r"\s+", " ", text.lower()).strip()


def _slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff_-]+", "-", value.strip()).strip("-")
    return slug[:48] or "memory"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
