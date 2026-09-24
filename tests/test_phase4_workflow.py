from pathlib import Path

from review_agent.graph.workflow import build_review_workflow, run_review_workflow
from review_agent.models.pr import PRLocator, PRMeta
from review_agent.services.review_service import ReviewService
from review_agent.tools.github_tools import GitHubClient


class FixtureGitHubClient(GitHubClient):
    def __init__(self, diff: str) -> None:
        self.diff = diff

    def fetch_pr_meta(self, locator: PRLocator) -> PRMeta:
        return PRMeta(
            owner=locator.owner,
            repo=locator.repo,
            pr_number=locator.pr_number,
            title="Fixture",
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


def test_workflow_builds_context_bundles(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "app.py").write_text("def greet(name):\n    return name\n", encoding="utf-8")
    diff = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,3 @@
 def greet(name):
+    name = name.strip()
     return name
"""
    service = ReviewService(github_client=FixtureGitHubClient(diff), repo_root=repo)
    state = run_review_workflow("https://github.com/octo/demo/pull/1", service=service)

    assert state["pr_meta"] is not None
    assert state["diff_hunks"]
    assert state["context_requests"]
    assert state["context_bundles"]
    assert build_review_workflow() is not None
