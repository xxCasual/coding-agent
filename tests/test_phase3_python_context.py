from pathlib import Path

from review_agent.services.repo_cache import RepoCache
from review_agent.tools.python_analysis_tools import (
    extract_imports_tool,
    extract_signatures_tool,
    find_enclosing_symbol_tool,
    python_ast_summary_tool,
)
from review_agent.tools.repo_tools import find_related_tests_tool, repo_tree_tool, search_symbol_tool


def test_repo_tools_scan_tree_symbols_and_related_tests(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    source = tmp_path / "src" / "pkg" / "maths.py"
    test_file = tmp_path / "tests" / "test_maths.py"
    source.write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    test_file.write_text("from pkg.maths import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")

    summary = repo_tree_tool(tmp_path)
    refs = search_symbol_tool(tmp_path, "add")
    related = find_related_tests_tool(tmp_path, "src/pkg/maths.py", "add")

    assert "src/pkg/maths.py" in summary.python_files
    assert "tests" in summary.test_directories
    assert any(ref.file_path.endswith("maths.py") for ref in refs)
    assert related and related[0].file_path == "tests/test_maths.py"


def test_python_ast_tools_extract_structure(tmp_path: Path) -> None:
    file_path = tmp_path / "module.py"
    file_path.write_text(
        "import os\nfrom typing import Any\n\nclass Box:\n    def value(self, x: Any) -> Any:\n        return x\n",
        encoding="utf-8",
    )

    summary = python_ast_summary_tool(file_path)
    enclosing = find_enclosing_symbol_tool(file_path, 5)

    assert extract_imports_tool(file_path) == ["import os", "from typing import Any"]
    assert "class Box" in extract_signatures_tool(file_path)
    assert "def value(self, x: Any) -> Any" in extract_signatures_tool(file_path)
    assert summary.classes[0].name == "Box"
    assert enclosing.name == "Box.value"


def test_repo_cache_uses_stable_directory_names(tmp_path: Path) -> None:
    cache = RepoCache(tmp_path / "cache")
    path = cache.path_for("octo", "demo", "abc123")
    assert path == tmp_path / "cache" / "octo__demo__abc123"
