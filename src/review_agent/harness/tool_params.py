from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from review_agent.harness.review_target import ReviewTarget
from review_agent.tools.github_tools import PR_URL_RE


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        return cls.model_json_schema()


class ListFilesParams(_StrictModel):
    path: str = "."
    limit: int = Field(default=200, ge=1, le=1000)


class ReadFileParams(_StrictModel):
    path: str
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @field_validator("path")
    @classmethod
    def path_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value

    @model_validator(mode="after")
    def end_must_not_precede_start(self) -> ReadFileParams:
        if self.start_line is not None and self.end_line is not None and self.end_line < self.start_line:
            raise ValueError("end_line must be >= start_line")
        return self


class SearchTextParams(_StrictModel):
    query: str
    path: str = "."
    limit: int = Field(default=50, ge=1, le=500)

    @field_validator("query")
    @classmethod
    def query_must_be_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("query must be a non-empty string")
        return value


class GitStatusParams(_StrictModel):
    pass


class GitDiffParams(_StrictModel):
    path: str | None = None


class RunCommandParams(_StrictModel):
    argv: list[str] = Field(min_length=1)
    cwd: str = "."
    timeout: int | None = Field(default=None, ge=1)

class ApplyPatchParams(_StrictModel):
    patch: str

    @field_validator("patch")
    @classmethod
    def patch_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("patch must be a non-empty unified diff")
        return value


class PythonAstSummaryParams(_StrictModel):
    path: str

    @field_validator("path")
    @classmethod
    def path_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value


class DelegateReviewParams(_StrictModel):
    run_id: str
    subtask_id: str
    workspace_revision: str
    focus: str = ""
    target: ReviewTarget = Field(default_factory=ReviewTarget)

    @field_validator("run_id", "subtask_id", "workspace_revision")
    @classmethod
    def required_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must be a non-empty string")
        return value.strip()


class FindingDispositionParams(_StrictModel):
    finding_id: str
    status: str
    reason: str

    @field_validator("finding_id", "reason")
    @classmethod
    def required_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("field must be a non-empty string")
        return value.strip()

    @field_validator("status")
    @classmethod
    def status_must_be_known(cls, value: str) -> str:
        if value not in {"fixed", "not_adopted"}:
            raise ValueError("status must be fixed or not_adopted")
        return value


class SubmitReviewResponseParams(_StrictModel):
    dispositions: list[FindingDispositionParams] = Field(min_length=1)


class ReviewPrParams(_StrictModel):
    pr_url: str

    @field_validator("pr_url")
    @classmethod
    def pr_url_must_be_github_pr(cls, value: str) -> str:
        stripped = value.strip()
        if not PR_URL_RE.match(stripped):
            raise ValueError(
                "pr_url must be a GitHub pull request URL like https://github.com/owner/repo/pull/123"
            )
        return stripped


class LoadSkillParams(_StrictModel):
    name: str

    @field_validator("name")
    @classmethod
    def name_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name is required")
        return value.strip()


class ReadSkillResourceParams(_StrictModel):
    name: str
    path: str

    @field_validator("name")
    @classmethod
    def name_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name is required")
        return value.strip()

    @field_validator("path")
    @classmethod
    def path_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value.strip()


class RunSkillScriptParams(_StrictModel):
    name: str
    path: str
    argv: list[str] = Field(default_factory=list)
    cwd: str = "."
    timeout: int | None = Field(default=None, ge=1)

    @field_validator("name")
    @classmethod
    def name_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name is required")
        return value.strip()

    @field_validator("path")
    @classmethod
    def path_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("path is required")
        return value.strip()


TOOL_PARAMS: dict[str, type[_StrictModel]] = {
    "list_files": ListFilesParams,
    "read_file": ReadFileParams,
    "search_text": SearchTextParams,
    "git_status": GitStatusParams,
    "git_diff": GitDiffParams,
    "python_ast_summary": PythonAstSummaryParams,
    "run_command": RunCommandParams,
    "apply_patch": ApplyPatchParams,
    "review_pr": ReviewPrParams,
    "delegate_review": DelegateReviewParams,
    "submit_review_response": SubmitReviewResponseParams,
    "load_skill": LoadSkillParams,
    "read_skill_resource": ReadSkillResourceParams,
    "run_skill_script": RunSkillScriptParams,
}
