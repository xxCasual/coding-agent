import logging
import time
import uuid
from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from review_agent.errors import ReviewAgentError, record_state_error
from review_agent.graph.nodes import (
    classify_change_node,
    context_fetch_node,
    context_planner_node,
    context_retry_node,
    cross_file_review_node,
    fetch_pr_node,
    lightweight_verify_node,
    parse_diff_node,
    rerank_findings_node,
    repo_index_node,
    review_hunks_node,
    should_cross_file_review,
    should_retry_context,
)
from review_agent.graph.state import ReviewState
from review_agent.observability import get_logger, log_event
from review_agent.services.review_context import ReviewRunContext

logger = get_logger(__name__)


def build_review_workflow(service=None):
    graph = StateGraph(ReviewState)
    graph.add_node("fetch_pr", _observed_node("fetch_pr", fetch_pr_node))
    graph.add_node("parse_diff", _observed_node("parse_diff", parse_diff_node))
    graph.add_node("repo_index", _observed_node("repo_index", repo_index_node))
    graph.add_node("classify_change", _observed_node("classify_change", classify_change_node))
    graph.add_node("context_planner", _observed_node("context_planner", context_planner_node))
    graph.add_node("context_fetch", _observed_node("context_fetch", context_fetch_node))
    graph.add_node("context_retry", _observed_node("context_retry", context_retry_node))
    graph.add_node("review_hunks", _observed_node("review_hunks", review_hunks_node))
    graph.add_node("cross_file_review", _observed_node("cross_file_review", cross_file_review_node))
    graph.add_node("lightweight_verify", _observed_node("lightweight_verify", lightweight_verify_node))
    graph.add_node("rerank_findings", _observed_node("rerank_findings", rerank_findings_node))

    graph.add_edge(START, "fetch_pr")
    graph.add_edge("fetch_pr", "parse_diff")
    graph.add_edge("parse_diff", "repo_index")
    graph.add_edge("repo_index", "classify_change")
    graph.add_edge("classify_change", "context_planner")
    graph.add_edge("context_planner", "context_fetch")
    graph.add_conditional_edges(
        "context_fetch",
        should_retry_context,
        {"retry": "context_retry", "done": "review_hunks"},
    )
    graph.add_edge("context_retry", "context_planner")
    graph.add_conditional_edges(
        "review_hunks",
        should_cross_file_review,
        {"cross_file": "cross_file_review", "done": "lightweight_verify"},
    )
    graph.add_edge("cross_file_review", "lightweight_verify")
    graph.add_edge("lightweight_verify", "rerank_findings")
    graph.add_edge("rerank_findings", END)
    return graph.compile()


def run_review_workflow(
    pr_url: str,
    service,
    review_id: str | None = None,
    run_context: ReviewRunContext | None = None,
) -> ReviewState:
    workflow = build_review_workflow(service)
    run_context = run_context or service.create_run_context(review_id)
    initial: ReviewState = {
        "pr_url": pr_url,
        "review_id": review_id,
        "trace_id": uuid.uuid4().hex,
        "pr_meta": None,
        "repo_summary": None,
        "diff_hunks": [],
        "context_requests": [],
        "context_bundles": {},
        "tool_evidence": [],
        "findings": [],
        "errors": [],
        "context_retry_count": 0,
        "_service": service,
        "_run_context": run_context,
    }
    return workflow.invoke(initial)


def _observed_node(
    stage: str, node: Callable[[ReviewState], ReviewState]
) -> Callable[[ReviewState], ReviewState]:
    def wrapped(state: ReviewState) -> ReviewState:
        started = time.perf_counter()
        trace_id = state.get("trace_id")
        review_id = state.get("review_id")
        log_event(
            logger,
            logging.INFO,
            "workflow.node.start",
            f"{stage} started",
            trace_id=trace_id,
            review_id=review_id,
            stage=stage,
        )
        try:
            result = node(state)
        except ReviewAgentError as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            record_state_error(state, exc, stage=stage, level="error")
            logger.exception(
                f"{stage} failed",
                extra={
                    "event": "workflow.node.failure",
                    "trace_id": trace_id,
                    "review_id": review_id,
                    "stage": stage,
                    "duration_ms": duration_ms,
                    "error_code": exc.code,
                },
            )
            raise
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            record_state_error(state, exc, stage=stage, level="error")
            logger.exception(
                f"{stage} failed",
                extra={
                    "event": "workflow.node.failure",
                    "trace_id": trace_id,
                    "review_id": review_id,
                    "stage": stage,
                    "duration_ms": duration_ms,
                    "error_code": "internal.unexpected",
                },
            )
            raise
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(
            logger,
            logging.INFO,
            "workflow.node.success",
            f"{stage} succeeded",
            trace_id=trace_id,
            review_id=review_id,
            stage=stage,
            duration_ms=duration_ms,
        )
        return result

    return wrapped
