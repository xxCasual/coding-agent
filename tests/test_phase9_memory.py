from review_agent.memory.router import ConversationRouter
from review_agent.models.finding import Finding
from review_agent.services.session_service import SessionService


def test_session_service_answers_follow_up_about_existing_findings() -> None:
    service = SessionService()
    finding = Finding(
        finding_id="F-1",
        hunk_id="h1",
        file_path="src/app.py",
        start_line=10,
        end_line=10,
        severity="high",
        category="security",
        title="Unsafe eval",
        evidence="eval(user_input)",
        explanation="Executes untrusted input.",
        suggestion="Use a parser.",
        confidence=0.95,
        is_blocking=True,
    )
    service.save_review("r1", [finding], "# Report")

    answer = service.chat("r1", "Why is finding 1 high severity?")
    filtered = service.chat("r1", "Show only security findings")

    assert "Unsafe eval" in answer
    assert "security" in filtered.lower()
    assert service.chat("other", "finding 1").startswith("No review session")


def test_conversation_router_classifies_supported_intents() -> None:
    router = ConversationRouter()
    assert router.route("Explain finding 2") == "explain_finding"
    assert router.route("Show only security findings") == "filter_findings"
    assert router.route("Generate a shorter PR comment version") == "refine_report"
    assert router.route("This function is internal only; reconsider") == "rerun_related_hunks"
