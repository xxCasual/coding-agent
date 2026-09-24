from dataclasses import dataclass, field
from pathlib import Path

from review_agent.errors import ErrorRecord


@dataclass
class ReviewRunContext:
    """Mutable state owned by one review workflow invocation."""

    review_id: str | None = None
    repo_root: Path | None = None
    warnings: list[ErrorRecord] = field(default_factory=list)
