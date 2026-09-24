from dataclasses import dataclass, field
from enum import Enum
from uuid import uuid4


class MemoryType(str, Enum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


class MemoryScope(str, Enum):
    WORKSPACE = "workspace"
    SESSION = "session"
    USER = "user"


@dataclass
class MemoryEntry:
    name: str
    description: str
    type: MemoryType
    content: str
    id: str = field(default_factory=lambda: uuid4().hex)
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    source: str = "agent"
    path: str | None = None
    scope: MemoryScope = MemoryScope.WORKSPACE
    related_file_hashes: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    user_confirmed: bool = False
    verification_status: str = "unverified"


@dataclass(frozen=True)
class MemoryIndexItem:
    id: str
    name: str
    description: str
    type: MemoryType
    path: str
    updated_at: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class LoadedMemories:
    index_markdown: str
    entries: list[MemoryEntry]
