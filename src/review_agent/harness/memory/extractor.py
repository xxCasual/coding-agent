from __future__ import annotations

from collections.abc import Callable
import re

from review_agent.harness.memory.models import MemoryEntry, MemoryScope, MemoryType
from review_agent.harness.memory.store import MemoryStore

MemoryCandidateExtractor = Callable[[str, str, list[str]], list[MemoryEntry]]


class MemoryExtractor:
    def __init__(
        self,
        store: MemoryStore,
        extractor: MemoryCandidateExtractor | None = None,
    ) -> None:
        self.store = store
        self.extractor = extractor

    def extract_after_turn(
        self,
        user_message: str,
        assistant_message: str = "",
        tool_summaries: list[str] | None = None,
    ) -> list[MemoryEntry]:
        tool_summaries = tool_summaries or []
        if self.extractor is not None:
            candidates = self.extractor(user_message, assistant_message, tool_summaries)
        else:
            candidates = _rule_based_candidates(user_message, tool_summaries)
        written: list[MemoryEntry] = []
        for candidate in candidates:
            saved = self.store.write_if_new(candidate)
            if saved is not None:
                written.append(saved)
        return written


def _rule_based_candidates(user_message: str, tool_summaries: list[str]) -> list[MemoryEntry]:
    candidates: list[MemoryEntry] = []
    for sentence in _sentences(user_message):
        normalized = _strip_memory_prefix(sentence)
        classified = _classify(normalized, original=sentence)
        if classified is None:
            continue
        memory_type, user_confirmed = classified
        candidates.append(
            _entry_from_sentence(
                normalized,
                memory_type,
                "user_turn",
                user_confirmed=user_confirmed,
            )
        )
    for summary in tool_summaries:
        # Failed tools / speculative summaries never become verified experience.
        if _looks_like_failure(summary):
            continue
        for sentence in _sentences(summary):
            classified = _classify(sentence, original=sentence)
            if classified is None:
                continue
            memory_type, _ = classified
            if memory_type in {MemoryType.REFERENCE}:
                candidates.append(
                    _entry_from_sentence(
                        sentence,
                        memory_type,
                        "tool_summary",
                        user_confirmed=False,
                    )
                )
    return candidates


def _classify(sentence: str, *, original: str | None = None) -> tuple[MemoryType, bool] | None:
    source = original or sentence
    lowered = sentence.lower()
    source_lowered = source.lower()
    explicit_remember = (
        "记住" in source
        or "remember" in source_lowered
        or source.strip().lower().startswith("please remember")
    )
    if _looks_like_reference(sentence):
        return MemoryType.REFERENCE, explicit_remember
    if any(phrase in sentence for phrase in {"我是", "我叫", "我的名字"}):
        return MemoryType.USER, True
    if any(phrase in lowered for phrase in {"i am ", "my name is", "call me"}):
        return MemoryType.USER, True
    if any(phrase in sentence for phrase in {"希望你", "以后", "偏好", "要求", "每次"}):
        return MemoryType.FEEDBACK, explicit_remember
    if any(phrase in lowered for phrase in {"prefer", "always", "please remember"}):
        return MemoryType.FEEDBACK, True
    # Plan/progress statements are not auto-promoted to verified project rules.
    if any(phrase in sentence for phrase in {"项目", "计划", "重构", "正在", "进度"}):
        if explicit_remember:
            return MemoryType.PROJECT, True
        return None
    if any(phrase in lowered for phrase in {"project", "refactor", "plan", "progress"}):
        if explicit_remember:
            return MemoryType.PROJECT, True
        return None
    if explicit_remember:
        return MemoryType.FEEDBACK, True
    return None


def _entry_from_sentence(
    sentence: str,
    memory_type: MemoryType,
    source: str,
    *,
    user_confirmed: bool,
) -> MemoryEntry:
    description = _truncate(sentence, 96)
    name = f"{memory_type.value}: {_truncate(sentence, 36)}"
    return MemoryEntry(
        name=name,
        description=description,
        type=memory_type,
        tags=[memory_type.value],
        source=source,
        content=sentence,
        scope=MemoryScope.WORKSPACE,
        related_file_hashes=[],
        evidence_refs=[],
        user_confirmed=user_confirmed,
        verification_status="unverified",
    )


def _sentences(text: str) -> list[str]:
    parts = re.split(r"[\n。！？!?；;]+", text)
    return [part.strip() for part in parts if part.strip()]


def _strip_memory_prefix(sentence: str) -> str:
    return re.sub(r"^(请)?(帮我)?(记住|remember)[:：\s]*", "", sentence, flags=re.IGNORECASE).strip()


def _looks_like_reference(sentence: str) -> bool:
    lowered = sentence.lower()
    if any(marker in sentence for marker in {"/", ".md", ".py", ".toml", ".json"}):
        return True
    return any(word in lowered for word in {"reference", "file", "path", "where"})


def _looks_like_failure(summary: str) -> bool:
    lowered = summary.lower()
    return any(
        marker in lowered
        for marker in {
            "failed",
            "failure",
            "error",
            "traceback",
            "exception",
            "success=false",
            "exit_code=",
        }
    )


def _truncate(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= limit:
        return clean
    return clean[: limit - 1].rstrip() + "…"
