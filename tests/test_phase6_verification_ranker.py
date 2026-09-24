from pathlib import Path

from review_agent.models.finding import Finding
from review_agent.reviewers.finding_ranker import dedupe_findings, rank_findings
from review_agent.tools.verification_tools import import_check_tool, python_syntax_check_tool


def finding(finding_id: str, severity: str, confidence: float) -> Finding:
    return Finding(
        finding_id=finding_id,
        hunk_id="h1",
        file_path="src/app.py",
        start_line=1,
        end_line=1,
        severity=severity,
        category="bug",
        title="Same issue",
        evidence="evidence",
        explanation="explanation",
        suggestion="suggestion",
        confidence=confidence,
    )


def test_verification_tools_record_success_and_failure(tmp_path: Path) -> None:
    good = tmp_path / "good.py"
    bad = tmp_path / "bad.py"
    good.write_text("import math\nx = math.sqrt(4)\n", encoding="utf-8")
    bad.write_text("def broken(:\n", encoding="utf-8")

    assert python_syntax_check_tool(good).success is True
    assert python_syntax_check_tool(bad).success is False
    assert import_check_tool(good).success is True


def test_ranker_dedupes_and_sorts_findings() -> None:
    findings = [finding("low", "low", 0.7), finding("high", "high", 0.5), finding("dupe", "high", 0.4)]
    ranked = rank_findings(dedupe_findings(findings))
    assert [f.finding_id for f in ranked] == ["high", "low"]
