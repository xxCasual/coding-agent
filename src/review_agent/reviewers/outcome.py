from dataclasses import dataclass, field
from collections.abc import Iterable

from review_agent.errors import ErrorRecord
from review_agent.models.finding import Finding


@dataclass(frozen=True)
class ReviewOutcome:
    findings: list[Finding] = field(default_factory=list)
    warnings: list[ErrorRecord] = field(default_factory=list)


def coerce_review_outcome(value: "ReviewOutcome | Iterable[Finding]") -> ReviewOutcome:
    if isinstance(value, ReviewOutcome):
        return value
    return ReviewOutcome(findings=list(value))
