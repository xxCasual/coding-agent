from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SkillSource = Literal["builtin", "project", "extra"]


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    license: str | None = None
    compatibility: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    allowed_tools: str | None = None
    unsupported_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillRecord:
    """Discovered skill; body is not loaded until selected."""

    name: str
    description: str
    root: Path
    source: SkillSource
    skill_path: Path
    meta: SkillMeta

    @property
    def skill_id(self) -> str:
        return self.name


@dataclass(frozen=True)
class LoadedSkill:
    record: SkillRecord
    body: str
    body_hash: str
    reference_hashes: dict[str, str] = field(default_factory=dict)
    frontmatter: dict[str, Any] = field(default_factory=dict)

    def event_payload(self, *, call_id: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "skill_id": self.record.skill_id,
            "name": self.record.name,
            "source": self.record.source,
            "source_path": str(self.record.skill_path),
            "body_hash": self.body_hash,
            "reference_hashes": dict(self.reference_hashes),
        }
        if call_id:
            payload["call_id"] = call_id
        if self.record.meta.allowed_tools:
            payload["allowed_tools_noted"] = self.record.meta.allowed_tools
            payload["allowed_tools_authoritative"] = False
        return payload
