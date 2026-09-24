import json
import logging
from pathlib import Path

import httpx
import pytest

from review_agent.config import Settings
from review_agent.errors import GitHubRequestError, InvalidInputError, record_state_error
from review_agent.graph.state import ReviewState
from review_agent.models.context import ContextBundle
from review_agent.models.diff import DiffHunk, DiffLine
from review_agent.models.finding import Finding
from review_agent.models.pr import PRLocator, PRMeta
from review_agent.observability import JsonLogFormatter
from review_agent.reviewers.hunk_reviewer import DeepSeekLLMReviewer
from review_agent.reviewers.outcome import ReviewOutcome
from review_agent.services.command_runner import CommandRunner
from review_agent.services.review_service import ReviewService
from review_agent.tools.github_tools import HttpGitHubClient, parse_pr_url_tool


def test_invalid_pr_url_uses_unified_error() -> None:
    with pytest.raises(InvalidInputError) as exc_info:
        parse_pr_url_tool("https://github.com/octo/demo/issues/123")

    assert exc_info.value.code == "input.invalid"
    assert "pull request URL" in exc_info.value.public_message


def test_state_error_records_render_as_review_warnings() -> None:
    state: ReviewState = {"errors": []}
    record = record_state_error(
        state,
        "Repository context is unavailable.",
        stage="context_fetch",
        level="warning",
        code="repo.context_unavailable",
    )

    assert state["errors"] == [record]
    assert record.public_message == "Repository context is unavailable."
    assert record.stage == "context_fetch"


def test_repo_materialization_failure_becomes_warning(tmp_path: Path) -> None:
    diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def parse(user_input):
+    return eval(user_input)
     return user_input
"""
    service = ReviewService(
        github_client=NoCloneGitHubClient(diff),
        settings=Settings(repo_cache_dir=(tmp_path / "cache").as_posix()),
        hunk_reviewer=StaticFindingReviewer(),
    )

    report = service.review_pr("https://github.com/octo/demo/pull/1")

    assert report.findings
    assert any("clone URL" in warning for warning in report.warnings)


def test_github_http_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def get(self, *args, **kwargs):
            request = httpx.Request("GET", "https://api.github.com/repos/octo/demo/pulls/1")
            raise httpx.ConnectError("network down", request=request)

    monkeypatch.setattr("review_agent.tools.github_tools.httpx.Client", FailingClient)

    with pytest.raises(GitHubRequestError) as exc_info:
        HttpGitHubClient(Settings()).fetch_pr_meta(PRLocator(owner="octo", repo="demo", pr_number=1))

    assert exc_info.value.code == "github.request_failed"
    assert exc_info.value.retryable is True


def test_llm_timeout_skips_findings_and_records_error() -> None:
    reviewer = DeepSeekLLMReviewer(
        settings=Settings(deepseek_api_key="test", review_agent_llm_timeout_seconds=0.1),
        client=TimeoutClient(),
    )
    hunk = DiffHunk(
        hunk_id="timeout",
        file_path="src/app.py",
        change_type="modified",
        new_start=1,
        raw_diff="",
        lines=[DiffLine(new_line_no=1, content="eval(user_input)", line_type="added")],
        added_code="eval(user_input)",
        language="python",
    )

    outcome = reviewer.review(hunk, ContextBundle(hunk_id="timeout", file_path="src/app.py"))

    assert outcome.findings == []
    assert outcome.warnings
    assert outcome.warnings[0].code == "llm.timeout"
    assert "LLM findings were skipped" in outcome.warnings[0].public_message


def test_command_runner_returns_failure_for_missing_command() -> None:
    result = CommandRunner().run(["definitely-not-a-real-review-agent-command-12345"], timeout=1)

    assert result.returncode == -1
    assert not result.success
    assert "No such file" in result.stderr or "FileNotFoundError" in result.stderr


def test_json_log_formatter_outputs_observability_fields() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord("review_agent.test", logging.ERROR, __file__, 1, "hello", (), None)
    record.event = "demo.failure"
    record.trace_id = "trace-1"
    record.review_id = "review-1"
    record.stage = "demo"
    record.duration_ms = 12.3
    record.error_code = "demo.failed"

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "demo.failure"
    assert payload["trace_id"] == "trace-1"
    assert payload["review_id"] == "review-1"
    assert payload["stage"] == "demo"
    assert payload["duration_ms"] == 12.3
    assert payload["error_code"] == "demo.failed"


class NoCloneGitHubClient:
    def __init__(self, diff: str) -> None:
        self.diff = diff

    def fetch_pr_meta(self, locator: PRLocator) -> PRMeta:
        return PRMeta(
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
            title="No clone",
            author="mona",
            base_ref="main",
            head_ref="feature",
            base_sha="a" * 40,
            head_sha="b" * 40,
            changed_files=["src/app.py"],
            html_url=f"https://github.com/{locator.owner}/{locator.repo}/pull/{locator.pr_number}",
        )

    def fetch_pr_diff(self, locator: PRLocator) -> str:
        return self.diff

    def fetch_file_at_ref(self, locator: PRLocator, file_path: str, ref: str) -> str:
        return ""


class TimeoutClient:
    class Chat:
        class Completions:
            def create(self, **kwargs):
                raise TimeoutError("too slow")

        def __init__(self) -> None:
            self.completions = self.Completions()

    def __init__(self) -> None:
        self.chat = self.Chat()


class StaticFindingReviewer:
    def review(self, hunk: DiffHunk, context: ContextBundle) -> ReviewOutcome:
        return ReviewOutcome(
            findings=[
                Finding(
                    finding_id="F-test",
                    hunk_id=hunk.hunk_id,
                    file_path=hunk.file_path,
                    start_line=hunk.new_start or 1,
                    end_line=hunk.new_start or 1,
                    severity="high",
                    category="security",
                    title="Unsafe dynamic code execution",
                    evidence="eval(user_input)",
                    explanation="Executes untrusted input.",
                    suggestion="Use a parser.",
                    confidence=0.9,
                    is_blocking=True,
                )
            ]
        )
