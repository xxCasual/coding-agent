from typing import Any, TypedDict

from review_agent.errors import ErrorRecord
from review_agent.models.context import ContextBundle, ContextRequest, RepoSummary, ToolEvidence
from review_agent.models.diff import DiffHunk
from review_agent.models.finding import Finding
from review_agent.models.pr import PRMeta
from review_agent.services.review_context import ReviewRunContext


class ReviewState(TypedDict, total=False):
    pr_url: str
    review_id: str | None
    raw_diff: str
    pr_meta: PRMeta | None
    repo_summary: RepoSummary | None
    diff_hunks: list[DiffHunk]
    context_requests: list[ContextRequest]
    context_bundles: dict[str, ContextBundle]
    tool_evidence: list[ToolEvidence]
    findings: list[Finding]
    errors: list[ErrorRecord]
    trace_id: str
    context_retry_count: int
    requires_cross_file_review: bool
    _service: Any
    _run_context: ReviewRunContext
