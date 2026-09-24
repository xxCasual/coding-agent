from pathlib import Path

from review_agent.config import Settings
from review_agent.harness.memory import AgentMemory, MemoryEntry, MemoryStore, MemoryType
from review_agent.harness.memory.extractor import MemoryExtractor
from review_agent.harness.memory.loader import MemoryLoader
from review_agent.harness.memory.organizer import MemoryOrganizer


def test_memory_store_initializes_index_and_round_trips_markdown(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.ensure_initialized()
    entry = MemoryEntry(
        id="m1",
        name="User preference",
        description="Prefer concise Chinese summaries",
        type=MemoryType.FEEDBACK,
        tags=["feedback"],
        content="用户偏好中文摘要。",
    )

    store.write_entry(entry)

    assert (tmp_path / ".memory" / "MEMORY.md").exists()
    loaded = store.get_entry("m1")
    assert loaded is not None
    assert loaded.type == MemoryType.FEEDBACK
    assert loaded.content == "用户偏好中文摘要。"
    assert "Prefer concise Chinese summaries" in store.index_markdown()


def test_memory_loader_selects_at_most_five_with_keyword_fallback(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    for index in range(8):
        store.write_entry(
            MemoryEntry(
                id=f"m{index}",
                name=f"memory item {index}",
                description="memory retrieval preference",
                type=MemoryType.FEEDBACK,
                content=f"memory content {index}",
            )
        )

    def failing_selector(*_args):
        raise RuntimeError("side query unavailable")

    loaded = MemoryLoader(store, max_loaded=5, selector=failing_selector).load_for_turn(
        "please use memory retrieval"
    )

    assert len(loaded.entries) == 5
    assert "Memory Index" in loaded.index_markdown


def test_memory_extractor_writes_user_feedback_reference_without_auto_project(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    extractor = MemoryExtractor(store)
    message = (
        "我是 xxcasual。"
        "希望你以后先读计划再动手。"
        "这个项目正在重构 Harness。"
        "参考文件在 /home/dev/notes/Harness.md。"
    )

    first = extractor.extract_after_turn(message)
    second = extractor.extract_after_turn(message)

    assert {entry.type for entry in first} == {
        MemoryType.USER,
        MemoryType.FEEDBACK,
        MemoryType.REFERENCE,
    }
    assert all(entry.verification_status == "unverified" for entry in first)
    assert second == []
    assert store.count_memory_files() == 3


def test_memory_organizer_compacts_duplicates_at_threshold(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    duplicate_a = MemoryEntry(
        id="a",
        name="same",
        description="same description",
        type=MemoryType.PROJECT,
        content="first line",
    )
    duplicate_b = MemoryEntry(
        id="b",
        name="same",
        description="same description",
        type=MemoryType.PROJECT,
        content="second line",
    )
    store.write_entry(duplicate_a)
    store.write_entry(duplicate_b)

    changed = MemoryOrganizer(store, file_threshold=2).compact_if_needed()

    assert changed is True
    entries = store.list_entries()
    assert len(entries) == 1
    assert "first line" in entries[0].content
    assert "second line" in entries[0].content


def test_memory_organizer_respects_existing_lock(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.write_entry(
        MemoryEntry(
            id="a",
            name="same",
            description="same description",
            type=MemoryType.PROJECT,
            content="first line",
        )
    )
    store.write_entry(
        MemoryEntry(
            id="b",
            name="same",
            description="same description",
            type=MemoryType.PROJECT,
            content="second line",
        )
    )
    store.lock_path.write_text("locked\n", encoding="utf-8")

    changed = MemoryOrganizer(store, file_threshold=2).compact_if_needed()

    assert changed is False
    assert store.lock_path.exists()
    assert store.count_memory_files() == 2


def test_agent_memory_hooks_load_extract_and_compact(tmp_path: Path) -> None:
    settings = Settings(
        review_agent_memory_dir=".memory",
        review_agent_memory_max_loaded=5,
        review_agent_memory_compact_file_threshold=10,
    )
    memory = AgentMemory(tmp_path, settings=settings)

    extracted = memory.extract_after_turn("希望你以后每次长任务结束前更新进度。")
    loaded = memory.load_for_turn("更新进度")

    assert extracted
    assert loaded.entries
    assert memory.compact_if_needed() is False
