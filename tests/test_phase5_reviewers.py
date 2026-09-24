from review_agent.models.context import ContextBundle
from review_agent.models.diff import DiffHunk, DiffLine
from review_agent.config import Settings
from review_agent.reviewers.cross_file_reviewer import cross_file_gate
from review_agent.reviewers.hunk_reviewer import DeepSeekLLMReviewer


class FakeOpenAIClient:
    def __init__(self, content: str) -> None:
        self.chat = FakeChat(content)


class FakeChat:
    def __init__(self, content: str) -> None:
        self.completions = FakeCompletions(content)


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content

    def create(self, **kwargs):
        return FakeResponse(self.content)


class FakeResponse:
    def __init__(self, content: str) -> None:
        self.choices = [FakeChoice(content)]


class FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


def test_deepseek_reviewer_normalizes_compact_json_findings() -> None:
    hunk = DiffHunk(
        hunk_id="probe.py:1:1",
        file_path="probe.py",
        change_type="added",
        new_start=2,
        new_end=2,
        raw_diff="@@",
        lines=[DiffLine(new_line_no=2, content="+value = eval(user_input)", line_type="added")],
        added_code="value = eval(user_input)",
        language="python",
    )
    client = FakeOpenAIClient(
        """
        {
          "findings": [
            {
              "file": "probe.py",
              "line": 2,
              "message": "eval executes user-controlled input",
              "severity": "high",
              "category": "security",
              "recommendation": "Replace eval with an explicit parser.",
              "confidence": 0.88,
              "blocking": true
            }
          ]
        }
        """
    )
    reviewer = DeepSeekLLMReviewer(
        settings=Settings(deepseek_api_key="test"),
        client=client,
    )

    outcome = reviewer.review(hunk, ContextBundle(hunk_id=hunk.hunk_id, file_path=hunk.file_path))

    assert outcome.warnings == []
    assert len(outcome.findings) == 1
    assert outcome.findings[0].hunk_id == hunk.hunk_id
    assert outcome.findings[0].file_path == "probe.py"
    assert outcome.findings[0].start_line == 2
    assert outcome.findings[0].title == "eval executes user-controlled input"
    assert outcome.findings[0].suggestion == "Replace eval with an explicit parser."
    assert outcome.findings[0].is_blocking is True


def test_deepseek_reviewer_without_api_key_skips_findings_with_warning() -> None:
    hunk = DiffHunk(
        hunk_id="probe.py:1:1",
        file_path="probe.py",
        change_type="added",
        new_start=2,
        new_end=2,
        raw_diff="@@",
        lines=[DiffLine(new_line_no=2, content="+value = eval(user_input)", line_type="added")],
        added_code="value = eval(user_input)",
        language="python",
    )
    reviewer = DeepSeekLLMReviewer(settings=Settings(deepseek_api_key=None))

    outcome = reviewer.review(hunk, ContextBundle(hunk_id=hunk.hunk_id, file_path=hunk.file_path))

    assert outcome.findings == []
    assert len(outcome.warnings) == 1
    assert outcome.warnings[0].code == "llm.unavailable"
    assert "LLM findings were skipped" in outcome.warnings[0].public_message


def test_cross_file_gate_detects_public_api_and_config_changes() -> None:
    signature_hunk = DiffHunk(
        hunk_id="h2",
        file_path="src/api.py",
        change_type="modified",
        raw_diff="@@\n-def load(a):\n+def load(a, b):",
        lines=[],
        added_code="def load(a, b):",
        removed_code="def load(a):",
        language="python",
    )
    config_hunk = DiffHunk(hunk_id="h3", file_path="pyproject.toml", change_type="modified", raw_diff="", lines=[])

    assert cross_file_gate([signature_hunk])
    assert cross_file_gate([config_hunk])
