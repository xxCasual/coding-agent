import re
from pathlib import Path

from review_agent.errors import RepositoryContextError, ReviewerUnavailableError, record_state_error
from review_agent.graph.context_fetcher import ContextFetcher, ContextFetchWarning
from review_agent.graph.state import ReviewState
from review_agent.models.context import ContextBundle, ContextRequest
from review_agent.reviewers.cross_file_policy import (
    any_needs_cross_file_review,
    cross_file_reason,
    needs_cross_file_review,
)
from review_agent.tools.diff_tools import parse_diff_hunks_tool
from review_agent.tools.github_tools import parse_pr_url_tool
from review_agent.tools.repo_tools import repo_tree_tool
from review_agent.reviewers.cross_file_reviewer import CrossFileReviewer
from review_agent.reviewers.finding_ranker import dedupe_findings, rank_findings
from review_agent.tools.verification_tools import import_check_tool, python_syntax_check_tool, pytest_targeted_tool, ruff_check_tool


def fetch_pr_node(state: ReviewState) -> ReviewState:
    service = state["_service"]
    run_context = state["_run_context"]
    locator = parse_pr_url_tool(state["pr_url"])
    state["pr_meta"] = service.github_client.fetch_pr_meta(locator)
    warning_count = len(run_context.warnings)
    service.materialize_pr_repo(state["pr_meta"], run_context)
    for warning in run_context.warnings[warning_count:]:
        record_state_error(state, warning, stage=warning.stage or "repo_materialize", level="warning")
    state["raw_diff"] = service.github_client.fetch_pr_diff(locator)
    return state


def parse_diff_node(state: ReviewState) -> ReviewState:
    state["diff_hunks"] = parse_diff_hunks_tool(state.get("raw_diff", ""))
    return state


def repo_index_node(state: ReviewState) -> ReviewState:
    repo_root = _repo_root(state)
    if repo_root and repo_root.exists():
        state["repo_summary"] = repo_tree_tool(repo_root)
    else:
        state["repo_summary"] = None
    return state


def classify_change_node(state: ReviewState) -> ReviewState:
    hunks = state.get("diff_hunks", [])
    state["requires_cross_file_review"] = any_needs_cross_file_review(hunks)
    return state


def context_planner_node(state: ReviewState) -> ReviewState:
    requests: list[ContextRequest] = []
    for hunk in state.get("diff_hunks", []):
        target_symbols = _symbols_from_hunk(hunk.added_code + "\n" + hunk.removed_code)
        requests.append(
            ContextRequest(
                request_id=f"ctx-{len(requests) + 1}",
                hunk_id=hunk.hunk_id,
                file_path=hunk.file_path,
                target_symbols=target_symbols,
                required_files=[hunk.file_path],
                need_enclosing_symbol=hunk.language == "python",
                need_imports=hunk.language == "python",
                need_callers=needs_cross_file_review(hunk),
                need_related_tests=hunk.language == "python",
                reason=cross_file_reason(hunk),
            )
        )
    state["context_requests"] = requests
    return state


def context_fetch_node(state: ReviewState) -> ReviewState:
    repo_root = _repo_root(state)
    fetcher = ContextFetcher(repo_root)
    hunks_by_id = {hunk.hunk_id: hunk for hunk in state.get("diff_hunks", [])}
    bundles: dict[str, ContextBundle] = {}
    for request in state.get("context_requests", []):
        bundle, warnings = fetcher.fetch(request, hunks_by_id.get(request.hunk_id))
        for warning in warnings:
            _record_context_warning(state, warning)
        bundles[request.hunk_id] = bundle
    state["context_bundles"] = bundles
    state["context_retry_count"] = state.get("context_retry_count", 0)
    return state


def review_hunks_node(state: ReviewState) -> ReviewState:
    service = state["_service"]
    findings = list(state.get("findings", []))
    bundles = state.get("context_bundles", {})
    for hunk in state.get("diff_hunks", []):
        context = bundles.get(hunk.hunk_id)
        if context is None:
            continue
        try:
            outcome = service.hunk_reviewer.review(hunk, context)
        except Exception as exc:
            record_state_error(
                state,
                ReviewerUnavailableError(
                    f"Hunk reviewer failed for {hunk.hunk_id}: {exc}",
                    public_message=f"Reviewer failed for {hunk.file_path}; that hunk was skipped.",
                    metadata={"hunk_id": hunk.hunk_id, "file_path": hunk.file_path},
                ),
                stage="review_hunks",
                level="warning",
            )
            continue
        findings.extend(outcome.findings)
        for warning in outcome.warnings:
            record_state_error(
                state,
                warning,
                stage=warning.stage or "review_hunks",
                level="warning",
            )
    state["findings"] = findings
    return state


def should_cross_file_review(state: ReviewState) -> str:
    if state.get("requires_cross_file_review", False):
        return "cross_file"
    return "done"


def cross_file_review_node(state: ReviewState) -> ReviewState:
    findings = list(state.get("findings", []))
    findings.extend(CrossFileReviewer().review(state.get("diff_hunks", []), state.get("context_bundles", {})))
    state["findings"] = findings
    return state


def lightweight_verify_node(state: ReviewState) -> ReviewState:
    repo_root = _repo_root(state)
    evidence = list(state.get("tool_evidence", []))
    if not repo_root or not repo_root.exists():
        state["tool_evidence"] = evidence
        return state

    changed_python_files = sorted(
        {
            hunk.file_path
            for hunk in state.get("diff_hunks", [])
            if hunk.language == "python" and (repo_root / hunk.file_path).exists()
        }
    )
    for file_path in changed_python_files:
        evidence.append(python_syntax_check_tool(repo_root / file_path))
        evidence.append(import_check_tool(repo_root / file_path))
    if changed_python_files:
        evidence.append(ruff_check_tool(repo_root, changed_python_files))

    related_tests = sorted(
        {
            ref.file_path
            for bundle in state.get("context_bundles", {}).values()
            for ref in bundle.related_tests
            if (repo_root / ref.file_path).exists()
        }
    )
    evidence.append(pytest_targeted_tool(repo_root, related_tests))
    state["tool_evidence"] = evidence
    return state


def rerank_findings_node(state: ReviewState) -> ReviewState:
    state["findings"] = rank_findings(dedupe_findings(state.get("findings", [])))
    return state


def should_retry_context(state: ReviewState) -> str:
    has_missing = any(bundle.missing_context for bundle in state.get("context_bundles", {}).values())
    if has_missing and state.get("context_retry_count", 0) < 1:
        return "retry"
    return "done"


def context_retry_node(state: ReviewState) -> ReviewState:
    state["context_retry_count"] = state.get("context_retry_count", 0) + 1
    return state


def _symbols_from_hunk(code: str) -> list[str]:
    symbols = set(re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", code))
    keywords = {"def", "class", "return", "if", "else", "for", "while", "import", "from", "True", "False", "None"}
    return sorted(symbol for symbol in symbols if symbol not in keywords)[:8]


def _record_context_warning(state: ReviewState, warning: ContextFetchWarning) -> None:
    record_state_error(
        state,
        RepositoryContextError(
            warning.message,
            public_message=warning.public_message,
            metadata={
                "hunk_id": warning.hunk_id,
                "file_path": warning.file_path,
                **warning.metadata,
            },
        ),
        stage="context_fetch",
        level="warning",
    )


def _repo_root(state: ReviewState) -> Path | None:
    repo_root = state["_run_context"].repo_root
    return Path(repo_root) if repo_root else None
