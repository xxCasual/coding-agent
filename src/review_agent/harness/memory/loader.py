from collections.abc import Callable
import re

from review_agent.harness.memory.models import LoadedMemories, MemoryEntry, MemoryIndexItem
from review_agent.harness.memory.store import MemoryStore

MemorySelector = Callable[[list[MemoryIndexItem], str, list[dict[str, str]]], list[str]]


class MemoryLoader:
    def __init__(
        self,
        store: MemoryStore,
        max_loaded: int = 5,
        selector: MemorySelector | None = None,
    ) -> None:
        self.store = store
        self.max_loaded = max_loaded
        self.selector = selector

    def load_for_turn(
        self,
        user_message: str,
        recent_messages: list[dict[str, str]] | None = None,
    ) -> LoadedMemories:
        recent_messages = recent_messages or []
        index_items = self.store.index_items()
        selected_ids = self._select_ids(index_items, user_message, recent_messages)
        entries_by_id = {entry.id: entry for entry in self.store.list_entries()}
        entries = [entries_by_id[memory_id] for memory_id in selected_ids if memory_id in entries_by_id]
        return LoadedMemories(
            index_markdown=self.store.index_markdown(),
            entries=entries[: self.max_loaded],
        )

    def _select_ids(
        self,
        index_items: list[MemoryIndexItem],
        user_message: str,
        recent_messages: list[dict[str, str]],
    ) -> list[str]:
        if self.selector is not None:
            try:
                selected = self.selector(index_items, user_message, recent_messages)
                return selected[: self.max_loaded]
            except Exception:
                pass
        return [
            item.id
            for item in _keyword_rank(index_items, user_message, recent_messages)[: self.max_loaded]
        ]


def _keyword_rank(
    index_items: list[MemoryIndexItem],
    user_message: str,
    recent_messages: list[dict[str, str]],
) -> list[MemoryIndexItem]:
    query = " ".join([user_message, *[message.get("content", "") for message in recent_messages]])
    tokens = _tokens(query)
    scored: list[tuple[int, MemoryIndexItem]] = []
    for item in index_items:
        haystack = _entry_text(item)
        score = sum(1 for token in tokens if token and token in haystack)
        if user_message.lower() in haystack and user_message:
            score += 2
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].updated_at), reverse=True)
    return [item for _, item in scored]


def _entry_text(item: MemoryIndexItem | MemoryEntry) -> str:
    tags = " ".join(item.tags)
    return f"{item.type.value} {item.name} {item.description} {tags}".lower()


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in re.findall(r"[A-Za-z0-9_\u4e00-\u9fff]+", text)}
