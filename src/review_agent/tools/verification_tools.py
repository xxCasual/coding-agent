import ast
import py_compile
import shutil
from pathlib import Path

from review_agent.models.context import ToolEvidence
from review_agent.services.command_runner import CommandRunner


def python_syntax_check_tool(file_path: str | Path) -> ToolEvidence:
    path = Path(file_path)
    try:
        ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return ToolEvidence(
            tool_name="python_syntax_check",
            success=False,
            summary=f"Syntax error at line {exc.lineno}: {exc.msg}",
            file_path=path.as_posix(),
            output=str(exc),
            verification_status="failed",
        )
    except OSError as exc:
        return ToolEvidence(
            tool_name="python_syntax_check",
            success=False,
            summary="Could not read file for Python syntax check",
            file_path=path.as_posix(),
            output=str(exc),
            verification_status="failed",
        )
    return ToolEvidence(
        tool_name="python_syntax_check",
        success=True,
        summary="Python syntax is valid",
        file_path=path.as_posix(),
        verification_status="passed",
    )


def import_check_tool(file_path: str | Path) -> ToolEvidence:
    path = Path(file_path)
    try:
        py_compile.compile(path.as_posix(), doraise=True)
    except py_compile.PyCompileError as exc:
        return ToolEvidence(
            tool_name="import_check",
            success=False,
            summary="Python compile/import precheck failed",
            file_path=path.as_posix(),
            output=str(exc),
            verification_status="failed",
        )
    except OSError as exc:
        return ToolEvidence(
            tool_name="import_check",
            success=False,
            summary="Python compile/import precheck could not read file",
            file_path=path.as_posix(),
            output=str(exc),
            verification_status="failed",
        )
    return ToolEvidence(
        tool_name="import_check",
        success=True,
        summary="Python compile/import precheck passed",
        file_path=path.as_posix(),
        verification_status="passed",
    )


def ruff_check_tool(repo_root: str | Path, file_paths: list[str]) -> ToolEvidence:
    if not shutil.which("ruff"):
        return ToolEvidence(
            tool_name="ruff_check",
            success=False,
            summary="ruff is not installed; skipped",
            verification_status="unverified",
        )
    command = ["ruff", "check", *file_paths]
    result = CommandRunner().run(command, cwd=repo_root, timeout=60)
    return ToolEvidence(
        tool_name="ruff_check",
        success=result.success,
        summary="ruff check passed" if result.success else "ruff check reported issues",
        command=" ".join(command),
        output=result.stdout + result.stderr,
        verification_status="passed" if result.success else "failed",
    )


def pytest_targeted_tool(repo_root: str | Path, test_paths: list[str]) -> ToolEvidence:
    if not test_paths:
        return ToolEvidence(
            tool_name="pytest_targeted",
            success=False,
            summary="No related tests discovered; skipped targeted pytest",
            verification_status="unverified",
        )
    command = ["pytest", *test_paths]
    result = CommandRunner().run(command, cwd=repo_root, timeout=120)
    return ToolEvidence(
        tool_name="pytest_targeted",
        success=result.success,
        summary="targeted pytest passed" if result.success else "targeted pytest failed",
        command=" ".join(command),
        output=result.stdout + result.stderr,
        verification_status="passed" if result.success else "failed",
    )
