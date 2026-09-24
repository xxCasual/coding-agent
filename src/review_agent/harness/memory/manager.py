from collections.abc import Callable
from pathlib import Path

from review_agent.config import Settings, get_settings
from review_agent.harness.memory.extractor import MemoryCandidateExtractor, MemoryExtractor
from review_agent.harness.memory.loader import MemoryLoader, MemorySelector
from review_agent.harness.memory.models import LoadedMemories, MemoryEntry
from review_agent.harness.memory.organizer import MemoryOrganizer
from review_agent.harness.memory.store import MemoryStore


class AgentMemory:
    """Turn-level memory hooks for the local harness agent."""

    def __init__(
        self,
        workspace_root: str | Path,
        settings: Settings | None = None,
        selector: MemorySelector | None = None,
        extractor: MemoryCandidateExtractor | None = None,
        *,
        memory_root: str | Path | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.enabled = self.settings.review_agent_memory_enabled
        self.store = MemoryStore(
            workspace_root,
            memory_dir=self.settings.review_agent_memory_dir,
            memory_root=memory_root,
        )
        self.loader = MemoryLoader(
            self.store,
            max_loaded=self.settings.review_agent_memory_max_loaded,
            selector=selector,
        )
        self.extractor = MemoryExtractor(self.store, extractor=extractor)
        self.organizer = MemoryOrganizer(
            self.store,
            file_threshold=self.settings.review_agent_memory_compact_file_threshold,
        )

    def load_for_turn(
        self,
        user_message: str,
        recent_messages: list[dict[str, str]] | None = None,
    ) -> LoadedMemories:
        if not self.enabled:
            return LoadedMemories(index_markdown="", entries=[])
        return self.loader.load_for_turn(user_message, recent_messages)

    def extract_after_turn(
        self,
        user_message: str,
        assistant_message: str = "",
        tool_summaries: list[str] | None = None,
    ) -> list[MemoryEntry]:
        if not self.enabled:
            return []
        return self.extractor.extract_after_turn(user_message, assistant_message, tool_summaries)

    def compact_if_needed(self) -> bool:
        if not self.enabled:
            return False
        return self.organizer.compact_if_needed()


MemoryHookFactory = Callable[[str | Path], AgentMemory]
