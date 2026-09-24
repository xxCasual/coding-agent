from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    runs: Mapped[list[RunRow]] = relationship(back_populates="session")


class RunRow(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint("task_mode IN ('develop', 'review', 'plan')", name="ck_runs_task_mode"),
        Index(
            "uq_runs_session_idempotency",
            "session_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        Index(
            "uq_runs_one_active_per_session",
            "session_id",
            unique=True,
            postgresql_where=text(
                "status IN ('queued', 'running', 'waiting_approval', 'interrupted', 'needs_attention')"
            ),
        ),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.session_id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requirement: Mapped[str] = mapped_column(Text, nullable=False)
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False, default="deepseek")
    task_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="develop", server_default="develop")
    review_target: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    acceptance: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    workspace_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verification: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    dispatch_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    budget: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    workspace_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    request_summary: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attention: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    review: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    session: Mapped[SessionRow] = relationship(back_populates="runs")


class MessageRow(Base):
    __tablename__ = "messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.session_id"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("runs.run_id"), nullable=True, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tool_calls: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    provider_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EventRow(Base):
    __tablename__ = "events"

    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id"), primary_key=True
    )
    seq: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ToolExecutionRow(Base):
    __tablename__ = "tool_executions"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id"), nullable=False, index=True
    )
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_call: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    call_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    argv: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    finished_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pgid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    container_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    container_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    workspace_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    stdout_artifact: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr_artifact: Mapped[str | None] = mapped_column(Text, nullable=True)
    patch_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    replay_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ApprovalRow(Base):
    __tablename__ = "approvals"

    approval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id"), nullable=False, index=True
    )
    call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    intent: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    param_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    workspace_revision: Mapped[str | None] = mapped_column(String(128), nullable=True)
    patch_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("run_id", "storage_path", name="uq_artifacts_run_path"),)

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("runs.run_id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
