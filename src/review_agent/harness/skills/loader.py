from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from review_agent.harness.skills.models import LoadedSkill, SkillRecord, SkillSource
from review_agent.harness.skills.parse import SkillParseError, parse_skill_meta, split_frontmatter

SkillLoadedCallback = Callable[[dict[str, Any]], None]

BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin"


class SkillLoader:
    """Discover and progressively load Agent Skills from configured roots only."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        builtin_root: str | Path | None = None,
        extra_roots: Sequence[str | Path] | None = None,
        on_loaded: SkillLoadedCallback | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.builtin_root = Path(builtin_root).resolve() if builtin_root else BUILTIN_ROOT
        self.extra_roots = [Path(root).expanduser().resolve() for root in (extra_roots or [])]
        self.on_loaded = on_loaded
        self._cache: dict[str, SkillRecord] | None = None

    def discover(self, *, refresh: bool = False) -> dict[str, SkillRecord]:
        if self._cache is not None and not refresh:
            return dict(self._cache)
        by_name: dict[str, SkillRecord] = {}
        # Lower priority first so higher priority overwrites.
        for root, source in self._iter_roots():
            for record in self._scan_root(root, source):
                by_name[record.name] = record
        self._cache = by_name
        return dict(by_name)

    def list_hints(self) -> list[dict[str, str]]:
        records = sorted(self.discover().values(), key=lambda item: item.name)
        return [
            {
                "name": record.name,
                "description": record.description,
                "source": record.source,
            }
            for record in records
        ]

    def get(self, name: str) -> SkillRecord | None:
        return self.discover().get(name)

    def load(self, name: str, *, call_id: str | None = None) -> LoadedSkill:
        record = self.get(name)
        if record is None:
            raise KeyError(f"unknown skill: {name}")
        text = record.skill_path.read_text(encoding="utf-8")
        frontmatter, body = split_frontmatter(text)
        # Re-validate on load so overrides stay consistent.
        parse_skill_meta(frontmatter, directory_name=record.root.name)
        body_hash = _sha256_text(body)
        loaded = LoadedSkill(
            record=record,
            body=body,
            body_hash=body_hash,
            reference_hashes={},
            frontmatter=frontmatter,
        )
        if self.on_loaded is not None:
            self.on_loaded(loaded.event_payload(call_id=call_id))
        return loaded

    def read_resource(self, name: str, relative_path: str) -> tuple[str, str]:
        """Return (text, content_hash) for a path under the skill root."""
        path = self.resolve_resource(name, relative_path)
        data = path.read_text(encoding="utf-8")
        return data, _sha256_text(data)

    def resolve_resource(self, name: str, relative_path: str) -> Path:
        record = self.get(name)
        if record is None:
            raise KeyError(f"unknown skill: {name}")
        return _resolve_under_root(record.root, relative_path)

    def resolve_script(self, name: str, relative_path: str) -> Path:
        path = self.resolve_resource(name, relative_path)
        rel = path.relative_to(self.get(name).root)  # type: ignore[union-attr]
        if rel.parts[0] != "scripts":
            raise ValueError("skill scripts must live under scripts/")
        if not path.is_file():
            raise FileNotFoundError(f"skill script not found: {relative_path}")
        return path

    def _iter_roots(self) -> Iterable[tuple[Path, SkillSource]]:
        if self.builtin_root.is_dir():
            yield self.builtin_root, "builtin"
        for root in self.extra_roots:
            if root.is_dir():
                yield root, "extra"
        project = self.workspace_root / ".agents" / "skills"
        if project.is_dir():
            yield project, "project"

    def _scan_root(self, root: Path, source: SkillSource) -> list[SkillRecord]:
        records: list[SkillRecord] = []
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            skill_md = child / "SKILL.md"
            if not skill_md.is_file():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
                frontmatter, _body = split_frontmatter(text)
                meta = parse_skill_meta(frontmatter, directory_name=child.name)
            except (OSError, SkillParseError, UnicodeDecodeError):
                continue
            records.append(
                SkillRecord(
                    name=meta.name,
                    description=meta.description,
                    root=child.resolve(),
                    source=source,
                    skill_path=skill_md.resolve(),
                    meta=meta,
                )
            )
        return records


def parse_extra_roots(value: str | None) -> list[Path]:
    if not value or not value.strip():
        return []
    roots: list[Path] = []
    for part in value.split(":"):
        part = part.strip()
        if part:
            roots.append(Path(part).expanduser())
    return roots


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _resolve_under_root(root: Path, value: str) -> Path:
    rel = Path(str(value or ""))
    if not str(value).strip():
        raise ValueError("path is required")
    if rel.is_absolute():
        raise ValueError("path must stay inside the skill root")
    if ".." in rel.parts:
        raise ValueError("path must stay inside the skill root")
    root = root.resolve()
    candidate = (root / rel)
    # Reject escaping symlinks at any component.
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            resolved = current.resolve(strict=False)
            if not _is_relative_to(resolved, root):
                raise ValueError("symlink must stay inside the skill root")
    resolved = candidate.resolve(strict=False)
    if not _is_relative_to(resolved, root):
        raise ValueError("path must stay inside the skill root")
    if not resolved.exists():
        raise FileNotFoundError(f"skill resource not found: {value}")
    return resolved


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
