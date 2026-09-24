import logging
import time
from pathlib import Path

from review_agent.config import Settings, get_settings
from review_agent.errors import ErrorRecord, RepositoryContextError, WorkflowError
from review_agent.models.pr import PRMeta
from review_agent.models.report import ReviewReport
from review_agent.reviewers.hunk_reviewer import HunkReviewer, build_default_hunk_reviewer
from review_agent.observability import get_logger, log_event
from review_agent.services.repo_cache import RepoCache
from review_agent.services.review_context import ReviewRunContext
from review_agent.tools.github_tools import GitHubClient, HttpGitHubClient

logger = get_logger(__name__)


class ReviewService:
    def __init__(
        self,
        github_client: GitHubClient | None = None,
        repo_root: str | Path | None = None,
        settings: Settings | None = None,
        hunk_reviewer: HunkReviewer | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.github_client = github_client or HttpGitHubClient(self.settings)
        self.repo_root = Path(repo_root) if repo_root else None
        self.hunk_reviewer = hunk_reviewer or build_default_hunk_reviewer(self.settings)

    def create_run_context(self, review_id: str | None = None) -> ReviewRunContext:
        return ReviewRunContext(review_id=review_id, repo_root=self.repo_root)

    def materialize_pr_repo(self, pr_meta: PRMeta, run_context: ReviewRunContext) -> Path | None:
        if run_context.repo_root and run_context.repo_root.exists():
            return run_context.repo_root
        clone_url = pr_meta.head_clone_url or pr_meta.clone_url
        if not clone_url:
            log_event(
                logger,
                logging.WARNING,
                "repo.materialize.failure",
                "Repository clone URL is unavailable",
                stage="repo_materialize",
                error_code="repo.context_unavailable",
                owner=pr_meta.owner,
                repo=pr_meta.repo,
                pr_number=pr_meta.pr_number,
            )
            run_context.warnings.append(
                RepositoryContextError(
                    "PR metadata did not include a clone URL; repository context is unavailable.",
                    public_message="PR metadata did not include a clone URL; repository context is unavailable.",
                    metadata={"owner": pr_meta.owner, "repo": pr_meta.repo, "pr_number": pr_meta.pr_number},
                ).to_record(level="warning", stage="repo_materialize")
            )
            return None
        started = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "repo.materialize.start",
            "Repository materialization started",
            stage="repo_materialize",
            owner=pr_meta.owner,
            repo=pr_meta.repo,
            pr_number=pr_meta.pr_number,
        )
        try:
            owner, repo = (pr_meta.head_repo_full_name or f"{pr_meta.owner}/{pr_meta.repo}").split("/", 1)
            run_context.repo_root = RepoCache(self.settings.repo_cache_dir).clone_or_update(
                clone_url,
                owner,
                repo,
                pr_meta.head_sha,
            )
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "Repository materialization failed",
                extra={
                    "event": "repo.materialize.failure",
                    "stage": "repo_materialize",
                    "duration_ms": duration_ms,
                    "error_code": "repo.context_unavailable",
                    "owner": pr_meta.owner,
                    "repo": pr_meta.repo,
                    "pr_number": pr_meta.pr_number,
                },
            )
            run_context.warnings.append(
                RepositoryContextError(
                    f"Failed to materialize repository context: {exc}",
                    public_message="Failed to materialize repository context.",
                    metadata={
                        "owner": pr_meta.owner,
                        "repo": pr_meta.repo,
                        "pr_number": pr_meta.pr_number,
                        "exception_type": type(exc).__name__,
                    },
                ).to_record(level="warning", stage="repo_materialize")
            )
            return None
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(
            logger,
            logging.INFO,
            "repo.materialize.success",
            "Repository materialization succeeded",
            stage="repo_materialize",
            duration_ms=duration_ms,
            owner=pr_meta.owner,
            repo=pr_meta.repo,
            pr_number=pr_meta.pr_number,
        )
        return run_context.repo_root

    def review_pr(self, pr_url: str, review_id: str | None = None) -> ReviewReport:
        from review_agent.graph.workflow import run_review_workflow
        from review_agent.tools.report_tools import markdown_report_renderer

        run_context = self.create_run_context(review_id)
        log_event(
            logger,
            logging.INFO,
            "review.start",
            "Review started",
            stage="review",
            review_id=review_id,
            pr_url=pr_url,
        )
        started = time.perf_counter()
        state = run_review_workflow(
            pr_url,
            service=self,
            review_id=review_id,
            run_context=run_context,
        )
        pr_meta = state["pr_meta"]
        if pr_meta is None:
            raise WorkflowError(
                "workflow did not fetch PR metadata",
                public_message="Review workflow did not fetch PR metadata.",
            )
        error_records = _dedupe_error_records([*state.get("errors", []), *run_context.warnings])
        hard_errors = [record for record in error_records if record.level == "error"]
        if hard_errors:
            first = hard_errors[0]
            raise WorkflowError(
                first.message,
                public_message=first.public_message,
                metadata={"error_code": first.code, "stage": first.stage},
            )
        warnings = [record.public_message for record in error_records if record.level == "warning"]
        report = markdown_report_renderer(
            pr_meta,
            state.get("findings", []),
            state.get("tool_evidence", []),
            warnings=warnings,
        )
        result = ReviewReport(
            pr_meta=pr_meta,
            findings=state.get("findings", []),
            final_report=report,
            tool_evidence=state.get("tool_evidence", []),
            warnings=warnings,
        )
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(
            logger,
            logging.INFO,
            "review.success",
            "Review succeeded",
            stage="review",
            trace_id=state.get("trace_id"),
            review_id=review_id,
            duration_ms=duration_ms,
            pr_url=pr_url,
            finding_count=len(result.findings),
            warning_count=len(warnings),
        )
        return result


def _dedupe_error_records(records: list[ErrorRecord]) -> list[ErrorRecord]:
    deduped: list[ErrorRecord] = []
    seen: set[tuple[str, str, str | None]] = set()
    for record in records:
        key = (record.code, record.public_message, record.stage)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(record)
    return deduped
