from collections import Counter

from review_agent.models.context import ToolEvidence
from review_agent.models.finding import Finding
from review_agent.models.pr import PRMeta


def markdown_report_renderer(
    pr_meta: PRMeta,
    findings: list[Finding],
    tool_evidence: list[ToolEvidence],
    warnings: list[str] | None = None,
) -> str:
    warnings = warnings or []
    blocking = [finding for finding in findings if finding.is_blocking]
    non_blocking = [finding for finding in findings if not finding.is_blocking]
    severity_counts = Counter(finding.severity for finding in findings)

    sections = [
        f"# Code Review: {pr_meta.owner}/{pr_meta.repo}#{pr_meta.pr_number}",
        "",
        f"- PR: [{pr_meta.title}]({pr_meta.html_url})",
        f"- Author: {pr_meta.author}",
        f"- Base: `{pr_meta.base_ref}` `{pr_meta.base_sha[:8]}`",
        f"- Head: `{pr_meta.head_ref}` `{pr_meta.head_sha[:8]}`",
        f"- Changed files: {len(pr_meta.changed_files)}",
        "",
        "## Risk Overview",
        "",
        f"- Total findings: {len(findings)}",
        f"- Blocking findings: {len(blocking)}",
        "- Severity mix: "
        + (", ".join(f"{severity}={count}" for severity, count in sorted(severity_counts.items())) or "none"),
        "",
        "## Blocking Findings",
        "",
        _render_findings(blocking) or "No blocking findings.",
        "",
        "## Non-Blocking Findings",
        "",
        _render_findings(non_blocking) or "No non-blocking findings.",
        "",
        "## Tool Evidence",
        "",
        _render_evidence(tool_evidence) or "No verification evidence recorded.",
    ]
    if warnings:
        sections.extend(["", "## Warnings", "", *[f"- {warning}" for warning in warnings]])
    return "\n".join(sections).rstrip() + "\n"


def _render_findings(findings: list[Finding]) -> str:
    parts: list[str] = []
    for index, finding in enumerate(findings, start=1):
        parts.extend(
            [
                f"### {index}. {finding.title}",
                "",
                f"- ID: `{finding.finding_id}`",
                f"- Location: `{finding.file_path}:{finding.start_line}`",
                f"- Severity: `{finding.severity}`",
                f"- Category: `{finding.category}`",
                f"- Confidence: {finding.confidence:.2f}",
                f"- Evidence: {finding.evidence}",
                f"- Explanation: {finding.explanation}",
                f"- Suggestion: {finding.suggestion}",
                "",
            ]
        )
    return "\n".join(parts).rstrip()


def _render_evidence(tool_evidence: list[ToolEvidence]) -> str:
    lines: list[str] = []
    for evidence in tool_evidence:
        status = _evidence_status_label(evidence)
        target = f" `{evidence.file_path}`" if evidence.file_path else ""
        lines.append(f"- `{evidence.tool_name}`{target}: {status} - {evidence.summary}")
    return "\n".join(lines)


def _evidence_status_label(evidence: ToolEvidence) -> str:
    if evidence.verification_status:
        mapping = {
            "passed": "passed",
            "failed": "reported issues",
            "unverified": "unverified",
            "not_applicable": "not_applicable",
        }
        return mapping.get(evidence.verification_status, evidence.verification_status)
    return "passed" if evidence.success else "reported issues"
