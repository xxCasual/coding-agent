from review_agent.models.finding import Finding
from review_agent.models.pr import PRMeta
from review_agent.tools.report_tools import markdown_report_renderer


def test_markdown_report_renderer_groups_findings() -> None:
    pr = PRMeta(
        owner="octo",
        repo="demo",
        pr_number=1,
        title="Fix",
        author="mona",
        base_ref="main",
        head_ref="feature",
        base_sha="a",
        head_sha="b",
        changed_files=["src/app.py"],
        html_url="https://github.com/octo/demo/pull/1",
    )
    report = markdown_report_renderer(
        pr,
        [
            Finding(
                finding_id="F-1",
                hunk_id="h1",
                file_path="src/app.py",
                start_line=2,
                end_line=2,
                severity="high",
                category="bug",
                title="Bad branch",
                evidence="line 2",
                explanation="can fail",
                suggestion="guard it",
                confidence=0.9,
                is_blocking=True,
            )
        ],
        [],
    )

    assert "# Code Review: octo/demo#1" in report
    assert "Blocking Findings" in report
    assert "Bad branch" in report
