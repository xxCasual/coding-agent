from dataclasses import dataclass, field

from review_agent.models.finding import Finding


@dataclass
class ConversationRecord:
    review_id: str
    findings: list[Finding] = field(default_factory=list)
    final_report: str = ""
    messages: list[dict[str, str]] = field(default_factory=list)
