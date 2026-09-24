from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Finding(BaseModel):
    finding_id: str
    hunk_id: str
    file_path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: Literal["critical", "high", "medium", "low", "info"]
    category: Literal[
        "bug",
        "security",
        "performance",
        "readability",
        "maintainability",
        "test",
        "style",
        "compatibility",
    ]
    title: str
    evidence: str
    explanation: str
    suggestion: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_blocking: bool = False

    @model_validator(mode="after")
    def line_range_is_ordered(self) -> "Finding":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self
