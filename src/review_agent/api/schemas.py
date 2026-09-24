import json

from pydantic import BaseModel, model_validator

from review_agent.harness.models import TaskMode
from review_agent.harness.review_target import ReviewTarget

from review_agent.models.finding import Finding
from review_agent.models.session import ReviewSession


class ReviewCreateRequest(BaseModel):
    pr_url: str


class ReviewCreateResponse(BaseModel):
    review_id: str
    thread_id: str
    status: str


class ReviewResponse(BaseModel):
    review_id: str
    thread_id: str
    pr_url: str
    status: str
    findings: list[Finding]
    final_report: str | None = None
    error: str | None = None

    @classmethod
    def from_session(cls, session: ReviewSession) -> "ReviewResponse":
        return cls(
            review_id=session.review_id,
            thread_id=session.thread_id,
            pr_url=session.pr_url,
            status=session.status,
            findings=[Finding.model_validate(item) for item in json.loads(session.findings_json)],
            final_report=session.final_report,
            error=session.error,
        )


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    answer: str
    review_id: str
    thread_id: str


class HealthResponse(BaseModel):
    status: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict | None = None


class WorkspaceOut(BaseModel):
    workspace_id: str
    display_name: str
    path: str


class SessionCreateRequest(BaseModel):
    workspace_id: str


class SessionOut(BaseModel):
    session_id: str
    workspace_id: str
    created_at: str


class AcceptanceIn(BaseModel):
    mode: str = "unverified"
    checks: list[list[str]] = []
    description: str = ""


class RunCreateRequest(BaseModel):
    requirement: str
    profile_id: str = "deepseek"
    acceptance: AcceptanceIn | None = None
    message_id: str | None = None
    reviewer: str = "default"
    task_mode: TaskMode = "develop"
    review_target: ReviewTarget | None = None

    @model_validator(mode="after")
    def check_task_options(self):
        from review_agent.harness.task_store import AcceptanceSpec, normalize_task_request
        spec = AcceptanceSpec(**self.acceptance.model_dump()) if self.acceptance else None
        normalize_task_request(self.task_mode, self.review_target, spec, self.reviewer)
        return self


class RunCreateResponse(BaseModel):
    task_mode: TaskMode = "develop"
    run_id: str
    session_id: str
    status: str


class PendingApprovalOut(BaseModel):
    approval_id: str
    intent: dict
    param_summary: str = ""
    workspace_revision: str | None = None
    patch_hash: str | None = None
    call_id: str | None = None


class RunOut(BaseModel):
    task_mode: TaskMode = "develop"
    review_target: ReviewTarget | None = None
    workspace_snapshot: dict | None = None
    run_id: str
    session_id: str
    status: str
    requirement: str
    profile_id: str
    phase: str | None = None
    verification: dict | None = None
    budget: dict | None = None
    usage: dict | None = None
    workspace_revision: str | None = None
    cancel_requested: bool = False
    dispatch_pending: bool = False
    pending_approval_id: str | None = None
    pending_approval: PendingApprovalOut | None = None
    needs_attention: bool = False
    attention: dict | None = None
    review: dict | None = None
    created_at: str
    updated_at: str


class MessageCreateRequest(BaseModel):
    content: str
    message_id: str | None = None


class MessageOut(BaseModel):
    message_id: str
    session_id: str
    run_id: str
    role: str
    content: str


class SessionDetailOut(BaseModel):
    session_id: str
    workspace_id: str
    created_at: str
    active_run_id: str | None = None
    messages: list[MessageOut]
    next_cursor: str | None = None


class CursorPage(BaseModel):
    items: list
    next_cursor: str | None = None


class ArtifactOut(BaseModel):
    artifact_id: str
    kind: str
    summary: str
    size_bytes: int
    content_hash: str


class ApprovalDecisionRequest(BaseModel):
    decision: str


class ResumeRequest(BaseModel):
    action: str = "continue"
    call_id: str | None = None
    workspace_revision: str | None = None
