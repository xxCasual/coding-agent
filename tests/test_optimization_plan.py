import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from review_agent.api.app import create_app
from review_agent.config import Settings
from review_agent.demo import write_demo_report
from review_agent.graph.context_fetcher import ContextFetcher
from review_agent.graph.nodes import context_planner_node
from review_agent.graph.state import ReviewState
from review_agent.models.context import ContextBundle, ContextRequest
from review_agent.models.diff import DiffHunk, DiffLine
from review_agent.models.finding import Finding
from review_agent.models.pr import PRLocator, PRMeta
from review_agent.reviewers.hunk_reviewer import DeepSeekLLMReviewer
from review_agent.reviewers.outcome import ReviewOutcome
from review_agent.services.review_service import ReviewService
from review_agent.services.review_store import ReviewStore
from review_agent.services.session_service import SessionService
from review_agent.tools.python_analysis_tools import python_ast_summary_tool
from review_agent.tools.github_tools import GitHubClient


class FixtureGitHubClient(GitHubClient):
    def __init__(self, repo_path: Path, diff: str) -> None:
        self.repo_path = repo_path
        self.diff = diff
        self.head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_path, text=True).strip()

    def fetch_pr_meta(self, locator: PRLocator) -> PRMeta:
        return PRMeta(
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
            title="Unsafe parser change",
            author="mona",
            base_ref="main",
            head_ref="feature",
            base_sha="0" * 40,
            head_sha=self.head_sha,
            changed_files=["src/app.py"],
            html_url=f"https://github.com/{locator.owner}/{locator.repo}/pull/{locator.pr_number}",
            clone_url=self.repo_path.as_posix(),
            head_clone_url=self.repo_path.as_posix(),
            head_repo_full_name=f"{locator.owner}/{locator.repo}",
        )

    def fetch_pr_diff(self, locator: PRLocator) -> str:
        return self.diff

    def fetch_file_at_ref(self, locator: PRLocator, file_path: str, ref: str) -> str:
        return (self.repo_path / file_path).read_text(encoding="utf-8")


def test_review_service_materializes_repo_and_runs_end_to_end(tmp_path: Path) -> None:
    source_repo = _make_git_repo(tmp_path / "source")
    diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def parse(user_input):
+    return eval(user_input)
     return user_input
"""
    github_client = FixtureGitHubClient(source_repo, diff)
    service = ReviewService(
        github_client=github_client,
        settings=Settings(repo_cache_dir=(tmp_path / "cache").as_posix()),
        hunk_reviewer=StaticFindingReviewer(),
    )

    report = service.review_pr("https://github.com/octo/demo/pull/1")

    cached_repo = tmp_path / "cache" / f"octo__demo__{github_client.head_sha}"
    assert (cached_repo / "src" / "app.py").exists()
    assert report.findings
    assert "Unsafe dynamic code execution" in report.final_report
    assert "Tool Evidence" in report.final_report


def test_review_service_run_contexts_are_isolated(tmp_path: Path) -> None:
    source_a = _make_git_repo(tmp_path / "source-a")
    source_b = _make_git_repo(tmp_path / "source-b")
    service = ReviewService(settings=Settings(repo_cache_dir=(tmp_path / "cache").as_posix()))
    run_a = service.create_run_context("review-a")
    run_b = service.create_run_context("review-b")

    service.materialize_pr_repo(_pr_meta("octo/demo-a", source_a), run_a)
    service.materialize_pr_repo(_pr_meta("octo/demo-b", source_b), run_b)

    assert run_a.repo_root is not None
    assert run_b.repo_root is not None
    assert run_a.repo_root != run_b.repo_root
    assert service.repo_root is None
    assert run_a.warnings == []
    assert run_b.warnings == []


def test_context_planner_marks_signature_class_delete_and_rename_as_cross_file() -> None:
    state: ReviewState = {
        "diff_hunks": [
            DiffHunk(
                hunk_id="sig",
                file_path="src/api.py",
                change_type="modified",
                raw_diff="",
                lines=[],
                added_code="def load(value, strict=False):",
                removed_code="def load(value):",
                language="python",
            ),
            DiffHunk(
                hunk_id="class",
                file_path="src/models.py",
                change_type="modified",
                raw_diff="",
                lines=[],
                added_code="class User(BaseModel):",
                language="python",
            ),
            DiffHunk(
                hunk_id="deleted",
                file_path="src/old.py",
                change_type="deleted",
                raw_diff="",
                lines=[],
                removed_code="def old():",
                language="python",
            ),
        ]
    }

    planned = context_planner_node(state)

    assert all(request.need_callers for request in planned["context_requests"])
    assert all(request.need_related_tests for request in planned["context_requests"])


def test_context_fetcher_respects_planner_flags(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text(
        "import os\n\n\ndef parse(value):\n    return value\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def search_symbol(repo_root, symbol):
        calls.append(f"search:{symbol}")
        return []

    def find_related_tests(repo_root, file_path, symbol):
        calls.append(f"tests:{symbol}")
        return []

    request = ContextRequest(
        request_id="ctx-1",
        hunk_id="h1",
        file_path="src/app.py",
        target_symbols=["parse"],
        need_imports=True,
        need_enclosing_symbol=True,
        need_callers=False,
        need_related_tests=False,
        reason="local context only",
    )
    hunk = DiffHunk(
        hunk_id="h1",
        file_path="src/app.py",
        change_type="modified",
        new_start=4,
        raw_diff="",
        lines=[],
        language="python",
    )

    bundle, warnings = ContextFetcher(
        repo,
        search_symbol=search_symbol,
        find_related_tests=find_related_tests,
    ).fetch(request, hunk)

    assert warnings == []
    assert bundle.imports == ["import os"]
    assert bundle.enclosing_symbol is not None
    assert calls == []


def test_context_fetcher_caches_ast_per_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def parse(value):\n    return value\n", encoding="utf-8")
    ast_calls = 0

    def ast_summary(path):
        nonlocal ast_calls
        ast_calls += 1
        return python_ast_summary_tool(path)

    fetcher = ContextFetcher(repo, ast_summary=ast_summary)
    request = ContextRequest(
        request_id="ctx-1",
        hunk_id="h1",
        file_path="src/app.py",
        target_symbols=[],
        need_imports=True,
        reason="local context",
    )
    hunk = DiffHunk(
        hunk_id="h1",
        file_path="src/app.py",
        change_type="modified",
        new_start=1,
        raw_diff="",
        lines=[],
        language="python",
    )

    fetcher.fetch(request, hunk)
    fetcher.fetch(request, hunk)

    assert ast_calls == 1


def test_deepseek_reviewer_skips_findings_when_model_returns_bad_json() -> None:
    reviewer = DeepSeekLLMReviewer(
        settings=Settings(deepseek_api_key="test"),
        client=BadJsonClient(),
    )
    hunk = DiffHunk(
        hunk_id="bad-json",
        file_path="src/app.py",
        change_type="modified",
        new_start=1,
        raw_diff="",
        lines=[DiffLine(new_line_no=1, content="eval(user_input)", line_type="added")],
        added_code="eval(user_input)",
        language="python",
    )

    outcome = reviewer.review(hunk, ContextBundle(hunk_id="bad-json", file_path="src/app.py"))

    assert outcome.findings == []
    assert outcome.warnings
    assert outcome.warnings[0].code == "llm.bad_response"
    assert "LLM findings were skipped" in outcome.warnings[0].public_message


def test_store_hydrates_session_service_for_cli_and_api_chat(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "reviews.sqlite3")
    review = store.create_review("https://github.com/octo/demo/pull/1")
    finding = _finding()
    store.save_success(review.review_id, [finding], "# Report")
    service = SessionService(store=store)

    answer = service.chat(review.review_id, "Explain finding 1")

    assert "Unsafe eval" in answer


def test_api_chat_uses_persisted_review_findings(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "reviews.sqlite3")
    review = store.create_review("https://github.com/octo/demo/pull/1")
    store.save_success(review.review_id, [_finding()], "# Report")
    client = TestClient(create_app(store=store, session_service=SessionService(store=store)))

    response = client.post(f"/api/reviews/{review.review_id}/chat", json={"message": "Explain finding 1"})

    assert response.status_code == 200
    assert "Unsafe eval" in response.json()["answer"]


def test_demo_command_writes_portfolio_report(tmp_path: Path) -> None:
    output = tmp_path / "demo-report.md"
    path = write_demo_report(output)

    assert path == output
    assert "Code Review" in output.read_text(encoding="utf-8")


class BadJsonClient:
    class Chat:
        class Completions:
            def create(self, **kwargs):
                return type(
                    "Response",
                    (),
                    {"choices": [type("Choice", (), {"message": type("Message", (), {"content": "not json"})()})()]},
                )()

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


def _make_git_repo(path: Path) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "src" / "app.py").write_text(
        "def parse(user_input):\n    return eval(user_input)\n", encoding="utf-8"
    )
    (path / "tests" / "test_app.py").write_text(
        "from src.app import parse\n\n\ndef test_parse():\n    assert parse('1') == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=path, check=True, capture_output=True)
    return path


def _pr_meta(full_name: str, repo_path: Path) -> PRMeta:
    owner, repo = full_name.split("/", 1)
    head_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo_path, text=True).strip()
    return PRMeta(
        owner=owner,
        repo=repo,
        pr_number=1,
        title="Fixture",
        author="mona",
        base_ref="main",
        head_ref="feature",
        base_sha="0" * 40,
        head_sha=head_sha,
        changed_files=["src/app.py"],
        html_url=f"https://github.com/{full_name}/pull/1",
        clone_url=repo_path.as_posix(),
        head_clone_url=repo_path.as_posix(),
        head_repo_full_name=full_name,
    )


def _finding() -> Finding:
    return Finding(
        finding_id="F-1",
        hunk_id="h1",
        file_path="src/app.py",
        start_line=1,
        end_line=1,
        severity="high",
        category="security",
        title="Unsafe eval",
        evidence="eval(user_input)",
        explanation="Executes untrusted input.",
        suggestion="Use a parser.",
        confidence=0.9,
        is_blocking=True,
    )
