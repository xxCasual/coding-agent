from typing import Any

from pydantic import BaseModel, Field


class FileSlice(BaseModel):
    file_path: str
    start_line: int
    end_line: int
    content: str


class SymbolReference(BaseModel):
    file_path: str
    line_no: int
    line: str


class EnclosingSymbol(BaseModel):
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str | None = None


class AstSymbol(BaseModel):
    name: str
    kind: str
    start_line: int
    end_line: int
    signature: str | None = None


class PythonAstSummary(BaseModel):
    file_path: str
    imports: list[str] = Field(default_factory=list)
    classes: list[AstSymbol] = Field(default_factory=list)
    functions: list[AstSymbol] = Field(default_factory=list)
    signatures: list[str] = Field(default_factory=list)


class RepoSummary(BaseModel):
    root: str
    python_files: list[str] = Field(default_factory=list)
    test_directories: list[str] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    package_roots: list[str] = Field(default_factory=list)


class ToolEvidence(BaseModel):
    tool_name: str
    success: bool
    summary: str
    command: str | None = None
    output: str | None = None
    file_path: str | None = None
    verification_status: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ContextRequest(BaseModel):
    request_id: str
    hunk_id: str
    file_path: str
    target_symbols: list[str] = Field(default_factory=list)
    required_files: list[str] = Field(default_factory=list)
    need_enclosing_symbol: bool = False
    need_imports: bool = False
    need_callers: bool = False
    need_related_tests: bool = False
    reason: str


class ContextBundle(BaseModel):
    hunk_id: str
    file_path: str
    file_slices: list[FileSlice] = Field(default_factory=list)
    enclosing_symbol: EnclosingSymbol | None = None
    ast_summary: PythonAstSummary | None = None
    imports: list[str] = Field(default_factory=list)
    symbol_references: list[SymbolReference] = Field(default_factory=list)
    related_tests: list[SymbolReference] = Field(default_factory=list)
    tool_evidence: list[ToolEvidence] = Field(default_factory=list)
    missing_context: list[str] = Field(default_factory=list)
