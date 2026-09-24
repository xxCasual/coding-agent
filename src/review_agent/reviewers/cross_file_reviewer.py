from review_agent.models.context import ContextBundle
from review_agent.models.diff import DiffHunk
from review_agent.models.finding import Finding
from review_agent.reviewers.cross_file_policy import any_needs_cross_file_review, needs_cross_file_review


def cross_file_gate(hunks: list[DiffHunk]) -> bool:
    return any_needs_cross_file_review(hunks)


class CrossFileReviewer:
    def review(self, hunks: list[DiffHunk], bundles: dict[str, ContextBundle]) -> list[Finding]:
        findings: list[Finding] = []
        for hunk in hunks:
            if not needs_cross_file_review(hunk):
                continue
            bundle = bundles.get(hunk.hunk_id)
            if not bundle or bundle.symbol_references or bundle.related_tests:
                continue
            findings.append(
                Finding(
                    finding_id=f"F-cross-{len(findings) + 1}",
                    hunk_id=hunk.hunk_id,
                    file_path=hunk.file_path,
                    start_line=hunk.new_start or 1,
                    end_line=hunk.new_start or 1,
                    severity="medium",
                    category="compatibility",
                    title="Cross-file impact needs confirmation",
                    evidence="The hunk changes a signature, import, config, dependency, deletion, or rename without fetched references.",
                    explanation="Public API and configuration changes can break callers outside the edited hunk.",
                    suggestion="Check callers, imports, and related tests before merging.",
                    confidence=0.55,
                )
            )
        return findings
