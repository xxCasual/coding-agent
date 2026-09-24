from __future__ import annotations

import re
from typing import Any

import yaml

from review_agent.harness.skills.models import SkillMeta

_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_KNOWN_FIELDS = frozenset(
    {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
)


class SkillParseError(ValueError):
    """Invalid SKILL.md frontmatter or naming."""


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise SkillParseError("SKILL.md must start with YAML frontmatter (---)")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillParseError("SKILL.md must start with YAML frontmatter (---)")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise SkillParseError("SKILL.md frontmatter is not closed")
    raw = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    try:
        data = yaml.safe_load(raw) if raw.strip() else {}
    except yaml.YAMLError as exc:
        raise SkillParseError(f"invalid YAML frontmatter: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise SkillParseError("frontmatter must be a YAML mapping")
    return data, body


def parse_skill_meta(frontmatter: dict[str, Any], *, directory_name: str) -> SkillMeta:
    unsupported = tuple(sorted(key for key in frontmatter if key not in _KNOWN_FIELDS))
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    if not isinstance(name, str) or not name.strip():
        raise SkillParseError("name is required")
    name = name.strip()
    if len(name) > 64:
        raise SkillParseError("name must be at most 64 characters")
    if not _NAME_RE.fullmatch(name):
        raise SkillParseError(
            "name must be lowercase alphanumeric with single hyphens (no leading/trailing/consecutive)"
        )
    if name != directory_name:
        raise SkillParseError(f"name {name!r} must match directory name {directory_name!r}")
    if not isinstance(description, str) or not description.strip():
        raise SkillParseError("description is required")
    description = description.strip()
    if len(description) > 1024:
        raise SkillParseError("description must be at most 1024 characters")

    license_value = frontmatter.get("license")
    if license_value is not None and not isinstance(license_value, str):
        raise SkillParseError("license must be a string")

    compatibility = frontmatter.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, str):
            raise SkillParseError("compatibility must be a string")
        if not (1 <= len(compatibility) <= 500):
            raise SkillParseError("compatibility must be 1-500 characters")

    metadata_raw = frontmatter.get("metadata") or {}
    if not isinstance(metadata_raw, dict):
        raise SkillParseError("metadata must be a mapping")
    metadata: dict[str, str] = {}
    for key, value in metadata_raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise SkillParseError("metadata keys and values must be strings")
        metadata[key] = value

    allowed_tools = frontmatter.get("allowed-tools")
    if allowed_tools is not None and not isinstance(allowed_tools, str):
        raise SkillParseError("allowed-tools must be a string")

    return SkillMeta(
        name=name,
        description=description,
        license=license_value,
        compatibility=compatibility,
        metadata=metadata,
        allowed_tools=allowed_tools,
        unsupported_fields=unsupported,
    )
