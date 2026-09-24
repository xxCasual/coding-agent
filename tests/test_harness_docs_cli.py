from pathlib import Path

from review_agent.cli import main


def test_harness_docs_and_standard_targets_exist() -> None:
    root = Path(__file__).resolve().parents[1]

    makefile = (root / "Makefile").read_text(encoding="utf-8")
    for target in {"setup:", "test:", "lint:", "check:", "agent:"}:
        assert target in makefile

    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    for ignored in {
        ".memory/",
        ".review-agent/",
        "AGENTS.md",
        "HARNESS_V1_PLAN.md",
        "PROGRESS.md",
        "CODE_REVIEW_AGENT_PLAN.md",
        "agent.md",
    }:
        assert ignored in gitignore


def test_cli_memory_list_initializes_workspace_memory(tmp_path: Path) -> None:
    result = main(["memory", "--cwd", tmp_path.as_posix(), "list"])

    assert result == 0
    assert (tmp_path / ".memory" / "MEMORY.md").exists()
