import re

from langgraph.checkpoint.memory import InMemorySaver

from review_agent.memory.conversation import ConversationRecord
from review_agent.memory.router import ConversationRouter
from review_agent.models.finding import Finding
from review_agent.services.review_store import ReviewStore


class SessionService:
    """Session-scoped memory for review follow-up conversations."""

    def __init__(self, store: ReviewStore | None = None) -> None:
        self.checkpointer = InMemorySaver()
        self.router = ConversationRouter()
        self.store = store
        self._records: dict[str, ConversationRecord] = {}

    def save_review(self, review_id: str, findings: list[Finding], final_report: str) -> None:
        self._records[review_id] = ConversationRecord(
            review_id=review_id,
            findings=findings,
            final_report=final_report,
        )

    def chat(self, review_id: str, message: str) -> str:
        record = self._records.get(review_id)
        if record is None and self.store is not None:
            record = self._hydrate_from_store(review_id)
        if record is None:
            return f"No review session found for {review_id}."

        intent = self.router.route(message)
        if intent == "explain_finding":
            answer = self._explain_finding(record, message)
        elif intent == "filter_findings":
            answer = self._filter_findings(record, message)
        elif intent == "refine_report":
            answer = self._refine_report(record)
        elif intent == "rerun_related_hunks":
            answer = self._rerun_related_hunks(record, message)
        else:
            answer = self._summarize(record)

        record.messages.append({"role": "user", "content": message})
        record.messages.append({"role": "assistant", "content": answer})
        return answer

    def _explain_finding(self, record: ConversationRecord, message: str) -> str:
        finding = self._select_finding(record, message)
        if finding is None:
            return "I could not find that finding in this review session."
        return (
            f"{finding.finding_id}: {finding.title}\n"
            f"Severity: {finding.severity}\n"
            f"Category: {finding.category}\n"
            f"Evidence: {finding.evidence}\n"
            f"Why it matters: {finding.explanation}\n"
            f"Suggested fix: {finding.suggestion}"
        )

    def _filter_findings(self, record: ConversationRecord, message: str) -> str:
        lowered = message.lower()
        matches = [
            finding
            for finding in record.findings
            if finding.category in lowered or finding.severity in lowered or finding.title.lower() in lowered
        ]
        if not matches:
            matches = record.findings
        if not matches:
            return "No findings are recorded for this review."
        return "\n".join(
            f"- {finding.finding_id} [{finding.severity}/{finding.category}] {finding.title}"
            for finding in matches
        )

    def _refine_report(self, record: ConversationRecord) -> str:
        blocking = [finding for finding in record.findings if finding.is_blocking]
        if not record.findings:
            return "Short PR comment: No actionable findings were recorded."
        lines = ["Short PR comment:"]
        for finding in blocking or record.findings[:3]:
            lines.append(f"- {finding.title} ({finding.severity}): {finding.suggestion}")
        return "\n".join(lines)

    def _rerun_related_hunks(self, record: ConversationRecord, message: str) -> str:
        finding = self._select_finding(record, message) or (record.findings[0] if record.findings else None)
        if finding is None:
            return "No findings are available to reconsider."
        return (
            f"I recorded your clarification for {finding.finding_id}. "
            "A full re-review would rerun the affected hunk with this note as user preference; "
            "in this MVP session, treat the finding as needing human confirmation."
        )

    def _summarize(self, record: ConversationRecord) -> str:
        if not record.findings:
            return "This review has no recorded findings."
        blocking = sum(1 for finding in record.findings if finding.is_blocking)
        return f"This review has {len(record.findings)} findings, including {blocking} blocking findings."

    def _select_finding(self, record: ConversationRecord, message: str) -> Finding | None:
        match = re.search(r"(?:finding\s*)?(\d+)", message.lower())
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(record.findings):
                return record.findings[index]
        for finding in record.findings:
            if finding.finding_id.lower() in message.lower() or finding.title.lower() in message.lower():
                return finding
        return record.findings[0] if record.findings else None

    def _hydrate_from_store(self, review_id: str) -> ConversationRecord | None:
        if self.store is None:
            return None
        review = self.store.get_review(review_id)
        if review is None or review.status != "succeeded":
            return None
        record = ConversationRecord(
            review_id=review_id,
            findings=self.store.get_findings(review_id),
            final_report=self.store.get_final_report(review_id),
        )
        self._records[review_id] = record
        return record
