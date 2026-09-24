from typing import Protocol

from review_agent.config import Settings, get_settings
from review_agent.models.context import ContextBundle
from review_agent.models.diff import DiffHunk
from review_agent.models.finding import Finding
from review_agent.reviewers.llm_reviewer import DeepSeekLLMReviewer
from review_agent.reviewers.outcome import ReviewOutcome, coerce_review_outcome


class LLMReviewer(Protocol):
    def review(self, hunk: DiffHunk, context: ContextBundle) -> ReviewOutcome | list[Finding]:
        ...


class HunkReviewer:
    def __init__(self, llm: LLMReviewer | None = None, settings: Settings | None = None) -> None:
        self.llm = llm or DeepSeekLLMReviewer(settings)

    def review(self, hunk: DiffHunk, context: ContextBundle) -> ReviewOutcome:
        return coerce_review_outcome(self.llm.review(hunk, context))


def build_default_hunk_reviewer(settings: Settings | None = None) -> HunkReviewer:
    settings = settings or get_settings()
    return HunkReviewer(DeepSeekLLMReviewer(settings))
