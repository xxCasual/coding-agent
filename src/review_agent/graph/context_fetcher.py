from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from review_agent.models.context import (
    ContextBundle,
    ContextRequest,
    EnclosingSymbol,
    FileSlice,
    PythonAstSummary,
    SymbolReference,
)
from review_agent.models.diff import DiffHunk
from review_agent.tools.python_analysis_tools import python_ast_summary_tool
from review_agent.tools.repo_tools import find_related_tests_tool, search_symbol_tool


@dataclass(frozen=True)
class ContextFetchWarning:
    hunk_id: str
    file_path: str
    message: str
    public_message: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ContextFetcher:
    def __init__(
        self,
        repo_root: str | Path | None,
        *,
        ast_summary: Callable[[str | Path], PythonAstSummary] = python_ast_summary_tool,
        search_symbol: Callable[[str | Path, str], list[SymbolReference]] = search_symbol_tool,
        find_related_tests: Callable[
            [str | Path, str, str | None], list[SymbolReference]
        ] = find_related_tests_tool,
    ) -> None:
        self.repo_root = Path(repo_root) if repo_root else None
        self._ast_summary = ast_summary
        self._search_symbol = search_symbol
        self._find_related_tests = find_related_tests
        self._file_lines_cache: dict[str, list[str]] = {}
        self._ast_cache: dict[str, PythonAstSummary] = {}

    def fetch(
        self,
        request: ContextRequest,
        hunk: DiffHunk | None,
    ) -> tuple[ContextBundle, list[ContextFetchWarning]]:
        bundle = ContextBundle(hunk_id=request.hunk_id, file_path=request.file_path)
        warnings: list[ContextFetchWarning] = []
        if not self.repo_root or not self.repo_root.exists():
            bundle.missing_context.append("repo_root is not available")
            warnings.append(
                ContextFetchWarning(
                    hunk_id=request.hunk_id,
                    file_path=request.file_path,
                    message="repo_root is not available",
                    public_message="Repository context is unavailable.",
                )
            )
            return bundle, warnings

        file_path = self.repo_root / request.file_path
        if not file_path.exists():
            message = f"{request.file_path} is not present in repo root"
            bundle.missing_context.append(message)
            warnings.append(
                ContextFetchWarning(
                    hunk_id=request.hunk_id,
                    file_path=request.file_path,
                    message=message,
                    public_message=f"{request.file_path} is not present in repo root.",
                )
            )
            return bundle, warnings

        self._fetch_file_slice(bundle, warnings, request, hunk)
        self._fetch_python_context(bundle, warnings, request, hunk)
        self._fetch_references(bundle, warnings, request)
        bundle.symbol_references = _dedupe_refs(bundle.symbol_references)
        bundle.related_tests = _dedupe_refs(bundle.related_tests)
        return bundle, warnings

    def _fetch_file_slice(
        self,
        bundle: ContextBundle,
        warnings: list[ContextFetchWarning],
        request: ContextRequest,
        hunk: DiffHunk | None,
    ) -> None:
        if hunk is None or not hunk.new_start:
            return
        try:
            lines = self._read_lines(request.file_path)
        except OSError as exc:
            _append_warning(
                bundle,
                warnings,
                request,
                "Could not read changed file context.",
                exc,
            )
            return
        start = max(hunk.new_start - 20, 1)
        end = min((hunk.new_end or hunk.new_start) + 20, len(lines))
        content = "\n".join(lines[start - 1 : end])
        bundle.file_slices.append(
            FileSlice(
                file_path=request.file_path,
                start_line=start,
                end_line=end,
                content=content,
            )
        )

    def _fetch_python_context(
        self,
        bundle: ContextBundle,
        warnings: list[ContextFetchWarning],
        request: ContextRequest,
        hunk: DiffHunk | None,
    ) -> None:
        if not request.file_path.endswith(".py"):
            return
        try:
            summary = self._python_ast_summary(request.file_path)
        except (OSError, SyntaxError) as exc:
            _append_warning(
                bundle,
                warnings,
                request,
                "Could not parse Python AST summary.",
                exc,
            )
            return

        bundle.ast_summary = summary
        if request.need_imports:
            bundle.imports = summary.imports
        if request.need_enclosing_symbol and hunk and hunk.new_start:
            bundle.enclosing_symbol = _find_enclosing_symbol(summary, hunk.new_start)

    def _fetch_references(
        self,
        bundle: ContextBundle,
        warnings: list[ContextFetchWarning],
        request: ContextRequest,
    ) -> None:
        if not self.repo_root:
            return
        if request.need_callers:
            for symbol in request.target_symbols:
                try:
                    bundle.symbol_references.extend(self._search_symbol(self.repo_root, symbol))
                except Exception as exc:
                    _append_warning(
                        bundle,
                        warnings,
                        request,
                        f"Could not search symbol references for {symbol}.",
                        exc,
                        {"symbol": symbol},
                    )
        if request.need_related_tests:
            for symbol in request.target_symbols:
                try:
                    bundle.related_tests.extend(
                        self._find_related_tests(self.repo_root, request.file_path, symbol)
                    )
                except Exception as exc:
                    _append_warning(
                        bundle,
                        warnings,
                        request,
                        f"Could not find related tests for {symbol}.",
                        exc,
                        {"symbol": symbol},
                    )

    def _read_lines(self, file_path: str) -> list[str]:
        if file_path not in self._file_lines_cache:
            if not self.repo_root:
                return []
            self._file_lines_cache[file_path] = (self.repo_root / file_path).read_text(
                encoding="utf-8"
            ).splitlines()
        return self._file_lines_cache[file_path]

    def _python_ast_summary(self, file_path: str) -> PythonAstSummary:
        if file_path not in self._ast_cache:
            if not self.repo_root:
                raise FileNotFoundError(file_path)
            self._ast_cache[file_path] = self._ast_summary(self.repo_root / file_path)
        return self._ast_cache[file_path]


def _find_enclosing_symbol(summary: PythonAstSummary, line_no: int) -> EnclosingSymbol | None:
    symbols = [*summary.classes, *summary.functions]
    candidates = [symbol for symbol in symbols if symbol.start_line <= line_no <= symbol.end_line]
    if not candidates:
        return None
    chosen = max(candidates, key=lambda symbol: symbol.start_line)
    return EnclosingSymbol(
        name=chosen.name,
        kind=chosen.kind,
        start_line=chosen.start_line,
        end_line=chosen.end_line,
        signature=chosen.signature,
    )


def _append_warning(
    bundle: ContextBundle,
    warnings: list[ContextFetchWarning],
    request: ContextRequest,
    public_message: str,
    exc: Exception,
    metadata: dict[str, Any] | None = None,
) -> None:
    message = f"{public_message} {exc}"
    bundle.missing_context.append(message)
    warnings.append(
        ContextFetchWarning(
            hunk_id=request.hunk_id,
            file_path=request.file_path,
            message=message,
            public_message=public_message,
            metadata=metadata or {},
        )
    )


def _dedupe_refs(refs: list[SymbolReference]) -> list[SymbolReference]:
    deduped: list[SymbolReference] = []
    seen: set[tuple[str, int, str]] = set()
    for ref in refs:
        key = (ref.file_path, ref.line_no, ref.line)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ref)
    return deduped
