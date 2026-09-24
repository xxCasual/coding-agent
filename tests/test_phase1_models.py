from review_agent.config import Settings
from review_agent.models.context import ContextBundle, ContextRequest
from review_agent.models.diff import DiffHunk, DiffLine
from review_agent.models.finding import Finding
from review_agent.models.pr import PRMeta
from review_agent.models.report import ReviewReport
from review_agent.models.session import ReviewSession


def test_core_models_validate_minimum_review_payload() -> None:
    pr = PRMeta(
        owner="octo",
        repo="demo",
        pr_number=7,
        title="Improve parser",
        author="mona",
        base_ref="main",
        head_ref="feature",
        base_sha="a" * 40,
        head_sha="b" * 40,
        changed_files=["src/parser.py"],
        html_url="https://github.com/octo/demo/pull/7",
    )
    hunk = DiffHunk(
        hunk_id="src/parser.py:10",
        file_path="src/parser.py",
        change_type="modified",
        old_start=10,
        old_end=12,
        new_start=10,
        new_end=13,
        raw_diff="@@ -10,3 +10,4 @@",
        lines=[DiffLine(new_line_no=10, content="+value = parse()", line_type="added")],
        language="python",
    )
    request = ContextRequest(
        request_id="ctx-1",
        hunk_id=hunk.hunk_id,
        file_path=hunk.file_path,
        target_symbols=["parse"],
        need_enclosing_symbol=True,
        reason="function body changed",
    )
    bundle = ContextBundle(hunk_id=hunk.hunk_id, file_path=hunk.file_path, imports=["import ast"])
    finding = Finding(
        finding_id="F-1",
        hunk_id=hunk.hunk_id,
        file_path=hunk.file_path,
        start_line=10,
        end_line=10,
        severity="high",
        category="bug",
        title="Parser can return None",
        evidence="parse() is used without checking None",
        explanation="Callers expect a string.",
        suggestion="Handle the None branch before returning.",
        confidence=0.8,
        is_blocking=True,
    )
    report = ReviewReport(pr_meta=pr, findings=[finding], final_report="# Review")
    session = ReviewSession(review_id="r1", thread_id="r1", pr_url=pr.html_url)

    assert report.pr_meta == pr
    assert request.required_files == []
    assert bundle.missing_context == []
    assert hunk.added_code == "value = parse()"
    assert session.status == "queued"


def test_settings_default_to_deepseek_v4_pro() -> None:
    settings = Settings()
    assert settings.deepseek_base_url == "https://api.deepseek.com"
    assert settings.deepseek_model == "deepseek-v4-pro"
