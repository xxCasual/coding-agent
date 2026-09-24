from __future__ import annotations

import os
from pathlib import Path

from review_agent.harness.memory.models import MemoryEntry
from review_agent.harness.memory.store import MemoryStore


class MemoryOrganizer:
    def __init__(self, store: MemoryStore, file_threshold: int = 10) -> None:
        self.store = store
        self.file_threshold = file_threshold

    def compact_if_needed(self) -> bool:
        self.store.ensure_initialized()
        if self.store.count_memory_files() < self.file_threshold:
            return False
        with _FileLock(self.store.lock_path) as acquired:
            if not acquired:
                return False
            return self._compact_duplicates()

    def _compact_duplicates(self) -> bool:
        groups: dict[tuple[str, str, str], list[MemoryEntry]] = {}
        for entry in self.store.list_entries():
            key = (
                entry.type.value,
                _normalize(entry.name),
                _normalize(entry.description),
            )
            groups.setdefault(key, []).append(entry)

        changed = False
        for entries in groups.values():
            if len(entries) < 2:
                continue
            keeper = entries[0]
            keeper.content = _merge_content(entries)
            keeper.tags = sorted({tag for entry in entries for tag in entry.tags})
            self.store.write_entry(keeper)
            for duplicate in entries[1:]:
                self.store.delete_entry(duplicate)
            changed = True
        self.store.rebuild_index()
        return changed


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None
        self.acquired = False

    def __enter__(self) -> bool:
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        self.acquired = True
        os.write(self.fd, b"locked\n")
        return True

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.acquired and self.path.exists():
            self.path.unlink()


def _merge_content(entries: list[MemoryEntry]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        for line in entry.content.splitlines() or [entry.content]:
            clean = line.strip()
            if clean and clean not in seen:
                seen.add(clean)
                lines.append(clean)
    return "\n".join(lines)


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())
