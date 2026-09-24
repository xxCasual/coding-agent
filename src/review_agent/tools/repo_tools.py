import os
import shutil
import subprocess
from pathlib import Path

from review_agent.models.context import FileSlice, RepoSummary, SymbolReference

CONFIG_NAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "tox.ini",
    ".ruff.toml",
}


def repo_tree_tool(repo_root: str | Path) -> RepoSummary:
    root = Path(repo_root)
    python_files: list[str] = []
    test_directories: set[str] = set()
    config_files: list[str] = []
    package_roots: set[str] = set()

    for path in root.rglob("*"):
        if _skip_path(path):
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_file() and path.suffix == ".py":
            python_files.append(rel)
            if "test" in path.parts or path.name.startswith("test_"):
                test_directories.add(path.parent.relative_to(root).as_posix())
        if path.is_dir() and path.name in {"tests", "test"}:
            test_directories.add(path.relative_to(root).as_posix())
        if path.is_file() and path.name in CONFIG_NAMES:
            config_files.append(rel)
        if path.is_file() and path.name == "__init__.py":
            package_roots.add(path.parent.relative_to(root).as_posix())

    return RepoSummary(
        root=root.as_posix(),
        python_files=sorted(python_files),
        test_directories=sorted(test_directories),
        config_files=sorted(config_files),
        package_roots=sorted(package_roots),
    )


def read_file_slice_tool(repo_root: str | Path, file_path: str, start_line: int, end_line: int) -> FileSlice:
    path = Path(repo_root) / file_path
    lines = path.read_text(encoding="utf-8").splitlines()
    start = max(start_line, 1)
    end = min(end_line, len(lines))
    content = "\n".join(lines[start - 1 : end])
    return FileSlice(file_path=file_path, start_line=start, end_line=end, content=content)


def search_symbol_tool(repo_root: str | Path, symbol: str) -> list[SymbolReference]:
    root = Path(repo_root)
    if shutil.which("rg"):
        result = subprocess.run(
            ["rg", "-n", "--no-heading", symbol, "."],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode in {0, 1}:
            return [_parse_rg_line(line) for line in result.stdout.splitlines() if line.strip()]
    return _search_symbol_python(root, symbol)


def find_related_tests_tool(
    repo_root: str | Path, file_path: str, symbol: str | None = None
) -> list[SymbolReference]:
    root = Path(repo_root)
    source_name = Path(file_path).stem
    needles = {source_name}
    if symbol:
        needles.add(symbol)
    refs: list[SymbolReference] = []
    for path in root.rglob("*.py"):
        if _skip_path(path):
            continue
        rel = path.relative_to(root).as_posix()
        if not _looks_like_test(path):
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if any(needle in line or needle in path.name for needle in needles):
                refs.append(SymbolReference(file_path=rel, line_no=line_no, line=line.strip()))
                break
    return refs


def _skip_path(path: Path) -> bool:
    return any(part in {".git", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache"} for part in path.parts)


def _looks_like_test(path: Path) -> bool:
    lowered = {part.lower() for part in path.parts}
    return "tests" in lowered or path.name.startswith("test_") or path.name.endswith("_test.py")


def _parse_rg_line(line: str) -> SymbolReference:
    file_path, line_no, content = line.split(":", 2)
    if file_path.startswith("./"):
        file_path = file_path[2:]
    return SymbolReference(file_path=file_path, line_no=int(line_no), line=content.strip())


def _search_symbol_python(root: Path, symbol: str) -> list[SymbolReference]:
    refs: list[SymbolReference] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {".git", ".venv", "__pycache__"}]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = Path(dirpath) / filename
            rel = path.relative_to(root).as_posix()
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if symbol in line:
                    refs.append(SymbolReference(file_path=rel, line_no=line_no, line=line.strip()))
    return refs
