from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DiffLine(BaseModel):
    old_line_no: int | None = None
    new_line_no: int | None = None
    content: str
    line_type: Literal["added", "removed", "context"]

    @property
    def code(self) -> str:
        if self.line_type == "added" and self.content.startswith("+"):
            return self.content[1:]
        if self.line_type == "removed" and self.content.startswith("-"):
            return self.content[1:]
        if self.line_type == "context" and self.content.startswith(" "):
            return self.content[1:]
        return self.content


class DiffHunk(BaseModel):
    hunk_id: str
    file_path: str
    change_type: Literal["added", "modified", "deleted", "renamed"]
    old_start: int | None = None
    old_end: int | None = None
    new_start: int | None = None
    new_end: int | None = None
    raw_diff: str
    lines: list[DiffLine] = Field(default_factory=list)
    added_code: str = ""
    removed_code: str = ""
    context_code: str = ""
    language: str | None = None
    enclosing_symbol: str | None = None

    @model_validator(mode="after")
    def populate_code_views(self) -> "DiffHunk":
        if not self.added_code:
            self.added_code = "\n".join(
                line.code.lstrip() for line in self.lines if line.line_type == "added"
            )
        if not self.removed_code:
            self.removed_code = "\n".join(
                line.code.lstrip() for line in self.lines if line.line_type == "removed"
            )
        if not self.context_code:
            self.context_code = "\n".join(
                line.code for line in self.lines if line.line_type == "context"
            )
        return self
