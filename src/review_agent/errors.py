from typing import Any, Literal, MutableMapping

from pydantic import BaseModel, Field

ErrorLevel = Literal["warning", "error"]


class ErrorRecord(BaseModel):
    code: str
    message: str
    public_message: str
    level: ErrorLevel
    stage: str | None = None
    retryable: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReviewAgentError(Exception):
    code = "review_agent.error"
    default_public_message = "Review failed."
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        public_message: str | None = None,
        retryable: bool | None = None,
        metadata: dict[str, Any] | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.public_message = public_message or self.default_public_message or message
        self.retryable = self.retryable if retryable is None else retryable
        self.metadata = _safe_metadata(metadata or {})
        self.code = code or self.code

    def to_record(self, *, level: ErrorLevel = "error", stage: str | None = None) -> ErrorRecord:
        return ErrorRecord(
            code=self.code,
            message=self.message,
            public_message=self.public_message,
            level=level,
            stage=stage,
            retryable=self.retryable,
            metadata=self.metadata,
        )


class InvalidInputError(ReviewAgentError):
    code = "input.invalid"
    default_public_message = "Invalid input."


class TaskServiceError(ReviewAgentError):
    code = "task.error"
    default_public_message = "Task request failed."
    http_status = 400


class IdempotencyConflictError(TaskServiceError):
    code = "task.idempotency_conflict"
    default_public_message = "Idempotency key was reused with a different request."
    http_status = 409


class SessionBusyError(TaskServiceError):
    code = "task.session_busy"
    default_public_message = "Session already has an active run."
    http_status = 409


class TaskNotFoundError(TaskServiceError):
    code = "task.not_found"
    default_public_message = "Resource not found."
    http_status = 404


class StageLimitedError(TaskServiceError):
    code = "task.stage_limited"
    default_public_message = "This control path is not scheduled until worker recovery is enabled."
    http_status = 409


class TerminalRunError(TaskServiceError):
    code = "task.terminal_run"
    default_public_message = "This run has ended; create a new run in the same session."
    http_status = 409


class InvalidWorkspaceError(TaskServiceError):
    code = "task.invalid_workspace"
    default_public_message = "Workspace is not registered."
    http_status = 400


class DatabaseUnavailableError(TaskServiceError):
    code = "task.database_unavailable"
    default_public_message = "Coding task database is not configured."
    http_status = 503


class ApprovalConflictError(TaskServiceError):
    code = "task.approval_conflict"
    default_public_message = "A different approval decision was already recorded."
    http_status = 409


class ResumeNotEligibleError(TaskServiceError):
    code = "task.resume_not_eligible"
    default_public_message = "This run cannot be resumed in its current state."
    http_status = 409


class ReconciliationRequiredError(TaskServiceError):
    code = "task.reconciliation_required"
    default_public_message = "Unknown side effects must be reconciled before the run can continue."
    http_status = 409


class ApprovalStaleError(TaskServiceError):
    code = "task.approval_stale"
    default_public_message = "Approval is bound to a different parameter or file version."
    http_status = 409


class RunLeaseHeldError(TaskServiceError):
    code = "task.lease_held"
    default_public_message = "Another worker currently holds this run."
    http_status = 409


class RunLockHeldError(TaskServiceError):
    code = "task.lock_held"
    default_public_message = "The run file lock is held by another live process."
    http_status = 409


class GitHubRequestError(ReviewAgentError):
    code = "github.request_failed"
    default_public_message = "Could not fetch pull request data from GitHub."
    retryable = True


class RepositoryContextError(ReviewAgentError):
    code = "repo.context_unavailable"
    default_public_message = "Repository context is unavailable."
    retryable = True


class WorkflowError(ReviewAgentError):
    code = "workflow.failed"
    default_public_message = "Review workflow failed."


class ToolExecutionError(ReviewAgentError):
    code = "tool.execution_failed"
    default_public_message = "A review tool failed."
    retryable = True


class ReviewerUnavailableError(ReviewAgentError):
    code = "llm.unavailable"
    default_public_message = "LLM reviewer is unavailable; LLM findings were skipped."
    retryable = True


def error_record_from_exception(
    error: Exception,
    *,
    level: ErrorLevel = "error",
    stage: str | None = None,
) -> ErrorRecord:
    if isinstance(error, ReviewAgentError):
        return error.to_record(level=level, stage=stage)
    return ErrorRecord(
        code="internal.unexpected",
        message=str(error),
        public_message="An unexpected error occurred.",
        level=level,
        stage=stage,
        retryable=False,
        metadata={"exception_type": type(error).__name__},
    )


def record_state_error(
    state: MutableMapping[str, Any],
    error: ErrorRecord | ReviewAgentError | Exception | str,
    *,
    stage: str,
    level: ErrorLevel = "warning",
    code: str = "workflow.warning",
    public_message: str | None = None,
    retryable: bool = False,
    metadata: dict[str, Any] | None = None,
) -> ErrorRecord:
    if isinstance(error, ErrorRecord):
        record = error.model_copy(update={"stage": error.stage or stage, "level": level})
    elif isinstance(error, ReviewAgentError):
        record = error.to_record(level=level, stage=stage)
    elif isinstance(error, Exception):
        record = error_record_from_exception(error, level=level, stage=stage)
    else:
        record = ErrorRecord(
            code=code,
            message=error,
            public_message=public_message or error,
            level=level,
            stage=stage,
            retryable=retryable,
            metadata=_safe_metadata(metadata or {}),
        )
    state.setdefault("errors", []).append(record)
    return record


def public_error_message(error: Exception) -> str:
    if isinstance(error, ReviewAgentError):
        return error.public_message
    return "Review failed unexpectedly. Check server logs for details."


def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        if isinstance(value, str | int | float | bool) or value is None:
            safe[key] = value
        elif isinstance(value, (list, tuple)):
            safe[key] = [
                item if isinstance(item, str | int | float | bool) or item is None else str(item)
                for item in value
            ]
        elif isinstance(value, dict):
            safe[key] = {
                str(inner_key): inner_value
                if isinstance(inner_value, str | int | float | bool) or inner_value is None
                else str(inner_value)
                for inner_key, inner_value in value.items()
            }
        else:
            safe[key] = str(value)
    return safe
