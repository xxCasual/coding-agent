import logging
from collections.abc import Callable

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

from review_agent.api.schemas import (
    ApprovalDecisionRequest,
    ArtifactOut,
    ChatRequest,
    ChatResponse,
    HealthResponse,
    MessageCreateRequest,
    MessageOut,
    PendingApprovalOut,
    ResumeRequest,
    RunCreateRequest,
    RunCreateResponse,
    RunOut,
    SessionCreateRequest,
    SessionDetailOut,
    SessionOut,
    ReviewCreateRequest,
    ReviewCreateResponse,
    ReviewResponse,
    WorkspaceOut,
)
from review_agent.demo import build_demo_artifacts
from review_agent.errors import DatabaseUnavailableError, ReviewAgentError, TaskServiceError, public_error_message
from review_agent.harness.task_store import AcceptanceSpec
from review_agent.observability import get_logger, log_event
from review_agent.services.review_store import ReviewStore
from review_agent.services.task_service import TaskService
from review_agent.tools.report_tools import markdown_report_renderer

logger = get_logger(__name__)
REVIEW_NOT_FOUND = "review not found"


def _task_http(exc: TaskServiceError) -> HTTPException:
    return HTTPException(
        status_code=getattr(exc, "http_status", 400),
        detail={"code": exc.code, "message": exc.public_message, "details": exc.metadata or None},
    )


def _require_task_service(task_service: TaskService | None) -> TaskService:
    if task_service is None:
        raise _task_http(
            DatabaseUnavailableError(
                "TaskService is not configured",
                public_message="Coding task database is not configured.",
            )
        )
    return task_service


def _pending_approval_out(payload: dict) -> PendingApprovalOut | None:
    record = payload.get("pending_approval")
    if record is None:
        return None
    return PendingApprovalOut(
        approval_id=record.approval_id,
        intent=dict(record.intent or {}),
        param_summary=record.param_summary or "",
        workspace_revision=record.workspace_revision,
        patch_hash=record.patch_hash,
        call_id=record.call_id,
    )


def _run_out(payload: dict) -> RunOut:
    run = payload["run"]
    pending = _pending_approval_out(payload)
    return RunOut(
        run_id=run.run_id,
        session_id=run.session_id,
        status=run.status.value if hasattr(run.status, "value") else str(run.status),
        requirement=run.requirement,
        profile_id=run.profile_id,
        task_mode=run.task_mode,
        review_target=run.review_target,
        workspace_snapshot=run.workspace_snapshot,
        phase=payload.get("phase"),
        verification=run.verification,
        budget=run.budget,
        usage=run.usage,
        workspace_revision=getattr(run, "workspace_revision", None),
        cancel_requested=run.cancel_requested,
        dispatch_pending=run.dispatch_pending,
        pending_approval_id=payload.get("pending_approval_id") or (pending.approval_id if pending else None),
        pending_approval=pending,
        needs_attention=bool(payload.get("needs_attention")),
        attention=payload.get("attention"),
        review=getattr(run, "review", None) or payload.get("review"),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def create_router(
    store: ReviewStore,
    service=None,
    session_service=None,
    service_factory: Callable | None = None,
    task_service: TaskService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @router.get("/demo-report", response_model=ReviewResponse)
    def demo_report() -> ReviewResponse:
        pr_meta, findings, evidence = build_demo_artifacts()
        return ReviewResponse(
            review_id="demo",
            thread_id="demo",
            pr_url=pr_meta.html_url,
            status="succeeded",
            findings=findings,
            final_report=markdown_report_renderer(pr_meta, findings, evidence),
            error=None,
        )

    @router.post("/reviews", response_model=ReviewCreateResponse)
    def create_review(request: ReviewCreateRequest, background_tasks: BackgroundTasks) -> ReviewCreateResponse:
        review = store.create_review(request.pr_url)
        background_tasks.add_task(
            _run_review_job,
            store,
            service,
            session_service,
            review.review_id,
            request.pr_url,
            service_factory,
        )
        return ReviewCreateResponse(
            review_id=review.review_id,
            thread_id=review.thread_id,
            status=review.status,
        )

    @router.get("/reviews/{review_id}", response_model=ReviewResponse)
    def get_review(review_id: str) -> ReviewResponse:
        review = store.get_review(review_id)
        if review is None:
            raise HTTPException(status_code=404, detail=REVIEW_NOT_FOUND)
        return ReviewResponse.from_session(review)

    @router.post("/reviews/{review_id}/chat", response_model=ChatResponse)
    def chat(review_id: str, request: ChatRequest) -> ChatResponse:
        review = store.get_review(review_id)
        if review is None:
            raise HTTPException(status_code=404, detail=REVIEW_NOT_FOUND)
        if session_service is not None:
            answer = session_service.chat(review_id, request.message)
        else:
            answer = "Chat memory is not configured for this app instance."
        return ChatResponse(answer=answer, review_id=review.review_id, thread_id=review.thread_id)

    @router.get("/workspaces", response_model=list[WorkspaceOut])
    def list_workspaces() -> list[WorkspaceOut]:
        svc = _require_task_service(task_service)
        return [
            WorkspaceOut(
                workspace_id=item.workspace_id,
                display_name=item.display_name,
                path=str(item.path),
            )
            for item in svc.list_workspaces()
        ]

    @router.post("/sessions", response_model=SessionOut)
    def create_session(request: SessionCreateRequest) -> SessionOut:
        svc = _require_task_service(task_service)
        try:
            record = svc.create_session(request.workspace_id)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return SessionOut(
            session_id=record.session_id,
            workspace_id=record.workspace_id,
            created_at=record.created_at,
        )

    @router.get("/sessions")
    def list_sessions(
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = None,
    ) -> dict:
        svc = _require_task_service(task_service)
        items, next_cursor = svc.list_sessions(limit=limit, cursor=cursor)
        return {
            "items": [
                SessionOut(
                    session_id=item.session_id,
                    workspace_id=item.workspace_id,
                    created_at=item.created_at,
                )
                for item in items
            ],
            "next_cursor": next_cursor,
        }

    @router.get("/sessions/{session_id}", response_model=SessionDetailOut)
    def get_session(
        session_id: str,
        limit: int = Query(default=50, ge=1, le=100),
        cursor: str | None = None,
    ) -> SessionDetailOut:
        svc = _require_task_service(task_service)
        try:
            payload = svc.get_session(session_id, message_limit=limit, message_cursor=cursor)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        session = payload["session"]
        return SessionDetailOut(
            session_id=session.session_id,
            workspace_id=session.workspace_id,
            created_at=session.created_at,
            active_run_id=payload["active_run_id"],
            messages=[
                MessageOut(
                    message_id=msg.message_id,
                    session_id=msg.session_id,
                    run_id=msg.run_id,
                    role=msg.role,
                    content=msg.content,
                )
                for msg in payload["messages"]
            ],
            next_cursor=payload["next_cursor"],
        )

    @router.get("/sessions/{session_id}/runs")
    def list_session_runs(
        session_id: str,
        limit: int = Query(default=20, ge=1, le=100),
        cursor: str | None = None,
    ) -> dict:
        svc = _require_task_service(task_service)
        try:
            items, next_cursor = svc.list_session_runs(session_id, limit=limit, cursor=cursor)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return {
            "items": [
                {
                    "run_id": item.run_id,
                    "status": item.status.value if hasattr(item.status, "value") else str(item.status),
                    "requirement": item.requirement,
                    "created_at": item.created_at,
                }
                for item in items
            ],
            "next_cursor": next_cursor,
        }

    @router.post("/sessions/{session_id}/runs", response_model=RunCreateResponse, status_code=202)
    def create_run(
        session_id: str,
        request: RunCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> RunCreateResponse:
        svc = _require_task_service(task_service)
        if not idempotency_key:
            raise HTTPException(
                status_code=400,
                detail={"code": "task.idempotency_required", "message": "Idempotency-Key header is required."},
            )
        acceptance = None
        if request.acceptance is not None:
            acceptance = AcceptanceSpec(
                mode=request.acceptance.mode,
                checks=list(request.acceptance.checks),
                description=request.acceptance.description,
            )
        try:
            run = svc.create_run(
                session_id,
                request.requirement,
                idempotency_key=idempotency_key,
                profile_id=request.profile_id,
                acceptance=acceptance,
                message_id=request.message_id,
                reviewer=request.reviewer,
                task_mode=request.task_mode,
                review_target=request.review_target,
            )
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return RunCreateResponse(run_id=run.run_id, session_id=run.session_id, status=run.status.value, task_mode=run.task_mode)

    @router.get("/runs/{run_id}", response_model=RunOut)
    def get_run(run_id: str) -> RunOut:
        svc = _require_task_service(task_service)
        try:
            return _run_out(svc.get_run(run_id))
        except TaskServiceError as exc:
            raise _task_http(exc) from exc

    @router.get("/runs/{run_id}/events")
    def stream_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        svc = _require_task_service(task_service)
        after_seq = after
        if last_event_id:
            try:
                after_seq = max(after_seq, int(last_event_id))
            except ValueError:
                after_seq = after
        try:
            iterator = svc.event_stream(run_id, after_seq=after_seq)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return StreamingResponse(iterator, media_type="text/event-stream")

    @router.post("/runs/{run_id}/messages", response_model=MessageOut)
    def append_message(run_id: str, request: MessageCreateRequest) -> MessageOut:
        svc = _require_task_service(task_service)
        try:
            stored = svc.append_message(run_id, request.content, message_id=request.message_id)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return MessageOut(
            message_id=stored.message_id,
            session_id=stored.session_id,
            run_id=stored.run_id,
            role=stored.role,
            content=stored.content,
        )

    @router.post("/runs/{run_id}/cancel", response_model=RunOut)
    def cancel_run(run_id: str) -> RunOut:
        svc = _require_task_service(task_service)
        try:
            svc.request_cancel(run_id)
            return _run_out(svc.get_run(run_id))
        except TaskServiceError as exc:
            raise _task_http(exc) from exc

    @router.post("/runs/{run_id}/resume")
    def resume_run(run_id: str, request: ResumeRequest | None = None) -> dict:
        svc = _require_task_service(task_service)
        body = request or ResumeRequest()
        try:
            run = svc.resume_run(
                run_id,
                action=body.action,
                call_id=body.call_id,
                workspace_revision=body.workspace_revision,
            )
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return {"ok": True, "run_id": run.run_id, "status": run.status.value}

    @router.post("/approvals/{approval_id}/decision")
    def approval_decision(approval_id: str, request: ApprovalDecisionRequest) -> dict:
        svc = _require_task_service(task_service)
        try:
            svc.decide_approval(approval_id, request.decision)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return {"ok": True}

    @router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactOut])
    def list_artifacts(run_id: str) -> list[ArtifactOut]:
        svc = _require_task_service(task_service)
        try:
            items = svc.list_artifacts(run_id)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return [
            ArtifactOut(
                artifact_id=item.artifact_id,
                kind=item.kind,
                summary=item.summary,
                size_bytes=item.size_bytes,
                content_hash=item.content_hash,
            )
            for item in items
        ]

    @router.get("/runs/{run_id}/artifacts/{artifact_id}")
    def get_artifact(run_id: str, artifact_id: str) -> FileResponse:
        svc = _require_task_service(task_service)
        try:
            record, path = svc.get_artifact_file(run_id, artifact_id)
        except TaskServiceError as exc:
            raise _task_http(exc) from exc
        return FileResponse(path, filename=record.summary)

    return router


def _run_review_job(
    store,
    service,
    session_service,
    review_id: str,
    pr_url: str,
    service_factory: Callable | None = None,
) -> None:
    log_event(
        logger,
        logging.INFO,
        "review_job.start",
        "Review job started",
        review_id=review_id,
        stage="review_job",
    )
    store.mark_running(review_id)
    active_service = service_factory() if service_factory is not None else service
    if active_service is None:
        store.save_failed(review_id, "ReviewService is not configured for this app instance.")
        return
    try:
        result = active_service.review_pr(pr_url, review_id=review_id)
    except ReviewAgentError as exc:
        store.save_failed(review_id, public_error_message(exc))
        logger.exception(
            "Review job failed",
            extra={
                "event": "review_job.failure",
                "review_id": review_id,
                "stage": "review_job",
                "error_code": exc.code,
            },
        )
        return
    except Exception as exc:  # pragma: no cover - defensive boundary around background jobs
        store.save_failed(review_id, public_error_message(exc))
        logger.exception(
            "Review job failed unexpectedly",
            extra={
                "event": "review_job.failure",
                "review_id": review_id,
                "stage": "review_job",
                "error_code": "internal.unexpected",
            },
        )
        return
    store.save_success(review_id, result.findings, result.final_report)
    if session_service is not None:
        session_service.save_review(review_id, result.findings, result.final_report)
    log_event(
        logger,
        logging.INFO,
        "review_job.success",
        "Review job succeeded",
        review_id=review_id,
        stage="review_job",
    )
