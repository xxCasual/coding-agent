from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    VERIFY = "verify"
    WRITE_CONFIRM = "write_confirm"
    DANGER_BLOCKED = "danger_blocked"


class ReplayCategory(str, Enum):
    REPEATABLE_READ = "repeatable_read"
    CONTROLLED_VERIFY = "controlled_verify"
    PATCH_CHECKABLE = "patch_checkable"
    NON_REPLAYABLE = "non_replayable"


class VerificationStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    NOT_APPLICABLE = "not_applicable"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationRecord(_FrozenModel):
    status: VerificationStatus
    command: str | None = None
    exit_code: int | None = None
    workspace_revision: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class AgentMessage:
    role: str
    content: str


class ToolCall(_FrozenModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
    provider_call_id: str | None = None


class Message(_FrozenModel):
    role: str
    content: str = ""
    message_id: str = ""
    session_id: str = ""
    run_id: str = ""
    tool_calls: list[ToolCall] | None = None
    provider_call_id: str | None = None


class Usage(_FrozenModel):
    profile_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    model_latency_ms: float | None = None
    estimated_cost: float | None = None
    price_config_id: str | None = None
    price_as_of: str | None = None


class ModelStreamEvent(_FrozenModel):
    type: str
    text: str = ""
    tool_call: ToolCall | None = None
    usage: Usage | None = None
    finish_reason: str | None = None
    error: str | None = None
    error_code: str | None = None


class ModelTurnResult(_FrozenModel):
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage | None = None
    finish_reason: str | None = None
    raw_error: str | None = None
    error_code: str | None = None


class ToolSpec(_FrozenModel):
    name: str
    description: str
    risk: ToolRisk
    input_schema: dict[str, Any]
    replay_category: ReplayCategory = ReplayCategory.REPEATABLE_READ


class ToolResult(_FrozenModel):
    success: bool
    summary: str
    stdout: str = ""
    stderr: str = ""
    changed_files: list[str] = Field(default_factory=list)
    verification_hint: str = ""
    risk_level: ToolRisk = ToolRisk.READ_ONLY
    call_id: str = ""
    error_code: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    verification: VerificationRecord | None = None

    def observation(self) -> str:
        parts = [f"success={self.success}", f"summary={self.summary}"]
        if self.call_id:
            parts.append(f"call_id={self.call_id}")
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        if self.verification is not None:
            parts.append(f"verification={self.verification.status.value}")
        if self.stdout:
            parts.append(f"stdout={self.stdout}")
        if self.stderr:
            parts.append(f"stderr={self.stderr}")
        if self.changed_files:
            parts.append(f"changed_files={', '.join(self.changed_files)}")
        if self.verification_hint:
            parts.append(f"verification_hint={self.verification_hint}")
        return "\n".join(parts)


class ApprovalRequest(_FrozenModel):
    tool_name: str
    arguments: dict[str, Any]
    reason: str
    workspace_revision: str | None = None
    patch_hash: str | None = None
    replay_category: str | None = None


class AgentEvent(_FrozenModel):
    type: str
    message: str
    tool_call: ToolCall | None = None
    tool_result: ToolResult | None = None
    approval_request: ApprovalRequest | None = None
    run_id: str = ""
    seq: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentFinal(_FrozenModel):
    message: str
    events: list[AgentEvent] = Field(default_factory=list)
    loaded_memory_count: int = 0
    session_id: str = ""
    run_id: str = ""
    verification: VerificationRecord | None = None
    run_status: str = ""


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    INTERRUPTED = "interrupted"
    NEEDS_ATTENTION = "needs_attention"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_RUN_STATUSES = frozenset(
    {
        RunStatus.QUEUED,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.INTERRUPTED,
        RunStatus.NEEDS_ATTENTION,
    }
)


ToolHandler = Callable[[dict[str, Any]], ToolResult | Awaitable[ToolResult]]


@dataclass(frozen=True)
class RegisteredTool:
    spec: ToolSpec
    handler: ToolHandler
    params_model: type[BaseModel] | None = None


TaskMode = Literal["develop", "review", "plan"]


class PlanResult(_FrozenModel):
    files: list[str]
    steps: list[str] = Field(min_length=1)
    validation: list[str] = Field(min_length=1)
    uncertainties: list[str]
