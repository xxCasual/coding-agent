from pathlib import Path

from review_agent.models.context import ToolEvidence
from review_agent.models.finding import Finding
from review_agent.models.pr import PRMeta
from review_agent.tools.report_tools import markdown_report_renderer


def write_demo_report(output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_demo_report(), encoding="utf-8")
    return path


def build_demo_report() -> str:
    pr_meta, findings, evidence = build_demo_artifacts()
    return markdown_report_renderer(pr_meta, findings, evidence)


def build_demo_artifacts() -> tuple[PRMeta, list[Finding], list[ToolEvidence]]:
    pr_meta = PRMeta(
        owner="portfolio",
        repo="demo-python-service",
        pr_number=42,
        title="Harden user input parsing",
        author="codex-demo",
        base_ref="main",
        head_ref="feature/review-agent-demo",
        base_sha="0" * 40,
        head_sha="1" * 40,
        changed_files=["src/app.py", "tests/test_app.py"],
        html_url="https://github.com/portfolio/demo-python-service/pull/42",
    )
    findings = [
        Finding(
            finding_id="F-demo-1",
            hunk_id="src/app.py:10:1",
            file_path="src/app.py",
            start_line=10,
            end_line=10,
            severity="high",
            category="security",
            title="Unsafe dynamic code execution",
            evidence="The added code calls eval(user_input).",
            explanation="Dynamic execution can run untrusted input with service privileges.",
            suggestion="Replace eval with an explicit parser or dispatch table.",
            confidence=0.9,
            is_blocking=True,
        )
    ]
    evidence = [
        ToolEvidence(
            tool_name="python_syntax_check",
            success=True,
            summary="Python syntax is valid",
            file_path="src/app.py",
        ),
        ToolEvidence(
            tool_name="pytest_targeted",
            success=True,
            summary="targeted pytest passed",
            command="pytest tests/test_app.py",
        ),
    ]
    return pr_meta, findings, evidence
