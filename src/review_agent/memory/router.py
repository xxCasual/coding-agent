class ConversationRouter:
    """Simple deterministic intent router for review follow-up messages."""

    def route(self, message: str) -> str:
        lowered = message.lower()
        if any(word in lowered for word in {"why", "explain", "severity", "高", "为什么", "解释"}):
            return "explain_finding"
        if any(phrase in lowered for phrase in {"show only", "filter", "only ", "只看", "筛选"}):
            return "filter_findings"
        if any(phrase in lowered for phrase in {"shorter", "summary", "pr comment", "简短", "精简"}):
            return "refine_report"
        if any(phrase in lowered for phrase in {"reconsider", "internal only", "rerun", "重新", "再评估"}):
            return "rerun_related_hunks"
        return "general"
