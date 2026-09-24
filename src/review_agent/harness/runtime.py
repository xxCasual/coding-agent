from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from review_agent.config import Settings, get_settings
from review_agent.harness.acceptance import AcceptanceGate
from review_agent.harness.approval import ApprovalPolicy
from review_agent.harness.coding_graph import CodingDeps, run_coding_graph
from review_agent.harness.model_client import ModelClient
from review_agent.services.graph_checkpointer import open_coding_checkpointer
from review_agent.harness.context import ContextAssembler, FragmentCache
from review_agent.harness.memory import AgentMemory
from review_agent.harness.models import AgentEvent, AgentFinal, ApprovalRequest, Message, RunStatus
from review_agent.harness.task_store import AcceptanceSpec, InMemoryTaskStore, TaskStore
from review_agent.harness.tools import ExecutionContext, ToolRegistry, build_default_tool_registry, build_reviewer_tool_registry
from review_agent.harness.review_target import ReviewTarget
from review_agent.harness.models import TaskMode
from review_agent.harness.workspace_revision import compute_workspace_revision
from review_agent.harness.skills import SkillLoader, parse_extra_roots
from review_agent.harness.mcp import McpSessionManager
from review_agent.services.executor import Executor
from review_agent.services.workspace_manager import RunWorkspace, WorkspaceManager, WorkspaceError

Approver = Callable[[ApprovalRequest], bool]
EventSink = Callable[[AgentEvent], None]
ToolRegistryFactory = Callable[..., ToolRegistry]


def _merge_extra_tools(base: ToolRegistry, extras: ToolRegistry | None) -> ToolRegistry:
    if extras is None or extras is base:
        return base
    for tool in extras.registered_tools():
        if base.get(tool.spec.name) is None:
            base.register(tool)
    return base


def _stable_workspace_id(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"ws-{digest}"


def _skill_loader_for(workspace_root: Path, settings: Settings) -> SkillLoader | None:
    if not getattr(settings, "review_agent_skills_enabled", True):
        return None
    return SkillLoader(
        workspace_root,
        extra_roots=parse_extra_roots(settings.review_agent_skills_extra_roots),
    )


class AgentRuntime:
    """Coding agent façade: session/run APIs over an explicit coding graph."""

    def __init__(
        self,
        workspace_root: str | Path,
        model_client: ModelClient,
        tool_registry: ToolRegistry | None = None,
        approval_policy: ApprovalPolicy | None = None,
        memory: AgentMemory | None = None,
        settings: Settings | None = None,
        task_store: TaskStore | None = None,
        *,
        profile_id: str = "deepseek",
        event_sink: EventSink | None = None,
        memory_root: str | Path | None = None,
        workspace_manager: WorkspaceManager | None = None,
        executor: Executor | None = None,
        isolate_workspace: bool = True,
        workspace_id: str | None = None,
        checkpointer: Any | None = None,
        tool_registry_factory: ToolRegistryFactory | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.settings = settings or get_settings()
        self.model_client = model_client
        self.isolate_workspace = isolate_workspace
        data_root = Path(self.settings.review_agent_data_root).expanduser()
        self.workspace_manager = workspace_manager or WorkspaceManager(data_root)
        self.workspace_id = workspace_id or _stable_workspace_id(self.workspace_root)
        self.workspace_manager.register(self.workspace_id, self.workspace_root)
        trusted_memory = memory_root or self.workspace_manager.memory_root_for(self.workspace_id)
        self.execution_context = ExecutionContext()
        self.executor = executor or Executor(
            backend=self.settings.review_agent_executor_backend,
            preview_chars=self.settings.review_agent_command_preview_chars,
            task_image=self.settings.review_agent_task_image,
            docker_network=self.settings.review_agent_docker_network,
        )
        self.skill_loader = _skill_loader_for(self.workspace_root, self.settings)
        self._tool_registry_factory = tool_registry_factory or build_default_tool_registry
        self._injected_tool_registry = tool_registry
        self._memory_injected = memory is not None
        self.tool_registry = tool_registry or self._tool_registry_factory(
            self.workspace_root,
            settings=self.settings,
            executor=self.executor,
            execution_context=self.execution_context,
            skill_loader=self.skill_loader,
        )
        self.approval_policy = approval_policy or ApprovalPolicy(
            self.settings.review_agent_approval_mode
        )
        if memory is not None:
            self.memory = memory
        else:
            self.memory = AgentMemory(
                self.workspace_root,
                settings=self.settings,
                memory_root=trusted_memory,
            )
        self.task_store: TaskStore = task_store or InMemoryTaskStore()
        self.profile_id = profile_id
        self.event_sink = event_sink
        self.fragment_cache = FragmentCache()
        self.context_assembler = ContextAssembler(
            self.workspace_root,
            fragment_cache=self.fragment_cache,
            reserve_output_tokens=self.settings.review_agent_context_reserve_output_tokens,
            optimize=bool(getattr(self.settings, "review_agent_context_optimization", True)),
        )
        self.acceptance_gate = AcceptanceGate(
            self.workspace_root,
            command_timeout=self.settings.review_agent_command_timeout_seconds,
            executor=self.executor,
        )
        self._default_session_id: str | None = None
        self._run_workspaces: dict[str, RunWorkspace] = {}
        self._mcp_by_run: dict[str, McpSessionManager] = {}
        self._memory_checkpointer: Any = checkpointer

    def create_session(self, workspace_id: str | None = None, *, session_id: str | None = None) -> str:
        record = self.task_store.create_session(
            workspace_id or self.workspace_id,
            session_id=session_id,
        )
        self._default_session_id = record.session_id
        return record.session_id

    def start_run(
        self,
        session_id: str,
        requirement: str,
        *,
        acceptance: AcceptanceSpec | None = None,
        profile_id: str | None = None,
        idempotency_key: str | None = None,
        run_id: str | None = None,
        reviewer: str = "default",
        task_mode: TaskMode = "develop",
        review_target: ReviewTarget | None = None,
    ) -> str:
        run = self.task_store.create_run(
            session_id,
            requirement,
            run_id=run_id,
            profile_id=profile_id or self.profile_id,
            acceptance=acceptance,
            idempotency_key=idempotency_key,
            reviewer=reviewer,
            task_mode=task_mode,
            review_target=review_target,
        )
        user = Message(
            role="user",
            content=requirement,
            message_id=str(uuid.uuid4()),
            session_id=session_id,
            run_id=run.run_id,
        )
        self.task_store.append_message(user)
        self.task_store.append_event(
            AgentEvent(
                type="run.created",
                message=f"Run created for session {session_id}",
                run_id=run.run_id,
                payload={"requirement": requirement, "task_mode": run.task_mode},
            )
        )
        return run.run_id

    def append_message(
        self,
        run_id: str,
        content: str,
        *,
        message_id: str | None = None,
        role: str = "user",
    ) -> Message:
        run = self.task_store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        if run.status not in {
            RunStatus.QUEUED,
            RunStatus.RUNNING,
            RunStatus.WAITING_APPROVAL,
            RunStatus.INTERRUPTED,
            RunStatus.NEEDS_ATTENTION,
        }:
            raise RuntimeError("cannot append message to terminal run; create a new run")
        message = Message(
            role=role,
            content=content,
            message_id=message_id or str(uuid.uuid4()),
            session_id=run.session_id,
            run_id=run_id,
        )
        stored = self.task_store.enqueue_pending(run_id, message)
        self.task_store.append_event(
            AgentEvent(
                type="message.received",
                message=f"Queued supplemental message {stored.message_id}",
                run_id=run_id,
                payload={"message_id": stored.message_id},
            )
        )
        if self.event_sink is not None:
            events = self.task_store.list_events(run_id)
            if events:
                self.event_sink(events[-1])
        return stored

    def request_cancel(self, run_id: str) -> None:
        run = self.task_store.get_run(run_id)
        already = bool(run and run.cancel_requested)
        self.task_store.update_run(run_id, cancel_requested=True)
        if not already:
            self.task_store.append_event(
                AgentEvent(type="run.cancel_requested", message="Cancel requested.", run_id=run_id)
            )
        self.propagate_cancel(run_id)

    def propagate_cancel(self, run_id: str) -> None:
        self.executor.cancel_run(run_id)
        manager = self._mcp_by_run.get(run_id)
        if manager is not None:
            manager.request_close()

    def get_run_workspace(self, run_id: str) -> RunWorkspace | None:
        return self._run_workspaces.get(run_id) or self.workspace_manager.get_run(run_id)

    def attach_or_prepare_workspace(self, run_id: str) -> RunWorkspace | None:
        if not self.isolate_workspace:
            return None
        existing = self.workspace_manager.get_run(run_id)
        if existing is not None and existing.source_root.exists():
            self._run_workspaces[run_id] = existing
            return existing
        return self.prepare_run_workspace(run_id)

    def prepare_run_workspace(self, run_id: str) -> RunWorkspace | None:
        if not self.isolate_workspace:
            return None
        prepared = prepare_run_source(self.task_store, self.workspace_manager, self.workspace_id, run_id)
        self._run_workspaces[run_id] = prepared
        return prepared

    async def execute_run(
        self,
        run_id: str,
        *,
        approver: Approver | None = None,
        resume: Any = None,
    ) -> AgentFinal:
        run = self.task_store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run: {run_id}")
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            raise RuntimeError(f"cannot execute terminal run: {run.status.value}")
        if run.status not in {RunStatus.WAITING_APPROVAL, RunStatus.NEEDS_ATTENTION}:
            self.task_store.update_run(run_id, status=RunStatus.RUNNING)

        prepared = self.attach_or_prepare_workspace(run_id)
        active_root = prepared.source_root if prepared is not None else self.workspace_root
        skill_loader = _skill_loader_for(active_root, self.settings) if run.task_mode == "develop" else None
        if prepared is not None:
            self.executor.set_artifact_root(prepared.artifacts_root)
            self.execution_context.run_id = run_id
            if run.task_mode == "develop":
                tool_registry = _merge_extra_tools(
                    self._tool_registry_factory(
                        active_root,
                        settings=self.settings,
                        executor=self.executor,
                        execution_context=self.execution_context,
                        skill_loader=skill_loader,
                    ),
                    self._injected_tool_registry,
                )
            else:
                tool_registry = build_reviewer_tool_registry(active_root, settings=self.settings,
                    executor=self.executor, execution_context=self.execution_context)
            context_assembler = ContextAssembler(
                active_root,
                fragment_cache=self.fragment_cache,
                reserve_output_tokens=self.settings.review_agent_context_reserve_output_tokens,
                optimize=bool(getattr(self.settings, "review_agent_context_optimization", True)),
            )
            acceptance_gate = AcceptanceGate(
                active_root,
                command_timeout=self.settings.review_agent_command_timeout_seconds,
                executor=self.executor,
                run_id=run_id,
            )
            if self._memory_injected:
                memory = self.memory
            else:
                memory = AgentMemory(
                    active_root,
                    settings=self.settings,
                    memory_root=prepared.memory_root,
                )
        else:
            tool_registry = (self.tool_registry if run.task_mode == "develop" else
                build_reviewer_tool_registry(active_root, settings=self.settings,
                    executor=self.executor, execution_context=self.execution_context))
            context_assembler = self.context_assembler
            acceptance_gate = self.acceptance_gate
            acceptance_gate.run_id = run_id
            memory = self.memory
            skill_loader = self.skill_loader if run.task_mode == "develop" else None
            self.execution_context.run_id = run_id

        async def model_complete(messages, tools=None, profile_id=None):
            task = asyncio.create_task(
                self.model_client.complete(
                    messages,
                    tools=tools,
                    profile_id=profile_id or run.profile_id,
                )
            )
            while not task.done():
                current = self.task_store.get_run(run_id)
                if current is not None and current.cancel_requested:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        raise
                    raise asyncio.CancelledError
                done, _pending = await asyncio.wait({task}, timeout=0.2)
                if done:
                    break
            return await task

        from review_agent.harness.tools import attach_review_tools
        from review_agent.harness.reviewer import reviewer_is_off

        def emit_review(event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
            event = self.task_store.append_event(
                AgentEvent(type=event_type, message=message, run_id=run_id, payload=payload or {})
            )
            if self.event_sink is not None:
                self.event_sink(event)

        if run.task_mode != "plan" and (run.task_mode == "review" or not reviewer_is_off(run)):
            attach_review_tools(
                tool_registry,
                workspace_root=active_root,
                store=self.task_store,
                model_complete=model_complete,
                settings=self.settings,
                emit=emit_review,
                profile_id=run.profile_id,
                execution_context=self.execution_context,
            )
        else:
            tool_registry.unregister("delegate_review")
            tool_registry.unregister("submit_review_response")
        if run.task_mode != "develop":
            tool_registry.restrict_read_only(allow_review=run.task_mode == "review")
            current_run = self.task_store.get_run(run_id)
            if not (current_run.workspace_snapshot or {}).get("source_revision"):
                self.task_store.update_run(run_id, workspace_snapshot={
                    **(current_run.workspace_snapshot or {}),
                    "source_revision": compute_workspace_revision(active_root),
                })
            revision = compute_workspace_revision(active_root)
            self.task_store.update_run(run_id, workspace_revision=revision, verification={
                "status": "not_applicable", "workspace_revision": revision,
                "command": None, "exit_code": None, "evidence_refs": [],
            })

        mcp_manager = (
            McpSessionManager.from_settings(
                self.settings, artifacts_root=prepared.artifacts_root if prepared is not None else None,
            )
            if run.task_mode == "develop" else McpSessionManager([])
        )
        self._mcp_by_run[run_id] = mcp_manager

        def emit_mcp(event_type: str, message: str, payload: dict[str, Any]) -> None:
            event = self.task_store.append_event(
                AgentEvent(type=event_type, message=message, run_id=run_id, payload=payload)
            )
            if self.event_sink is not None:
                self.event_sink(event)

        try:
            if run.task_mode == "develop":
                await mcp_manager.start(tool_registry, emit=emit_mcp)
            if not (self.settings.review_agent_database_url or "").strip():
                if self._memory_checkpointer is None:
                    from langgraph.checkpoint.memory import InMemorySaver

                    self._memory_checkpointer = InMemorySaver()
                saver = self._memory_checkpointer
                deps = CodingDeps(
                    workspace_root=active_root,
                    store=self.task_store,
                    model_complete=model_complete,
                    tool_registry=tool_registry,
                    approval_policy=self.approval_policy,
                    memory=memory,
                    acceptance_gate=acceptance_gate,
                    context_assembler=context_assembler,
                    settings=self.settings,
                    fragment_cache=self.fragment_cache,
                    approver=approver,
                    event_sink=self.event_sink,
                    profile_id=run.profile_id,
                    checkpointer=saver,
                    executor=self.executor,
                    execution_context=self.execution_context,
                    workspace_manager=self.workspace_manager if prepared is not None else None,
                    run_workspace=prepared,
                    skill_loader=skill_loader,
                    mcp_manager=mcp_manager,
                )
                return await run_coding_graph(
                    deps,
                    session_id=run.session_id,
                    run_id=run.run_id,
                    requirement=run.requirement,
                    profile_id=run.profile_id,
                    resume=resume,
                )
            async with open_coding_checkpointer(self.settings.review_agent_database_url) as saver:
                deps = CodingDeps(
                    workspace_root=active_root,
                    store=self.task_store,
                    model_complete=model_complete,
                    tool_registry=tool_registry,
                    approval_policy=self.approval_policy,
                    memory=memory,
                    acceptance_gate=acceptance_gate,
                    context_assembler=context_assembler,
                    settings=self.settings,
                    fragment_cache=self.fragment_cache,
                    approver=approver,
                    event_sink=self.event_sink,
                    profile_id=run.profile_id,
                    checkpointer=saver,
                    executor=self.executor,
                    execution_context=self.execution_context,
                    workspace_manager=self.workspace_manager if prepared is not None else None,
                    run_workspace=prepared,
                    skill_loader=skill_loader,
                    mcp_manager=mcp_manager,
                )
                return await run_coding_graph(
                    deps,
                    session_id=run.session_id,
                    run_id=run.run_id,
                    requirement=run.requirement,
                    profile_id=run.profile_id,
                    resume=resume,
                )
        finally:
            had_servers = bool(mcp_manager.servers)
            await mcp_manager.close()
            self._mcp_by_run.pop(run_id, None)
            if had_servers:
                emit_mcp("mcp.disconnected", "MCP sessions closed", {"run_id": run_id})

    async def run_turn(
        self,
        user_message: str,
        recent_messages: list[dict[str, str]] | None = None,
        approver: Approver | None = None,
        *,
        session_id: str | None = None,
        acceptance: AcceptanceSpec | None = None,
        reviewer: str = "default",
        task_mode: TaskMode = "develop",
        review_target: ReviewTarget | None = None,
    ) -> AgentFinal:
        del recent_messages  # history now lives in TaskStore; kept for call-site compat
        sid = session_id or self._default_session_id
        if sid is None or self.task_store.get_session(sid) is None:
            sid = self.create_session()
        run_id = self.start_run(sid, user_message, acceptance=acceptance, reviewer=reviewer,
                                task_mode=task_mode, review_target=review_target)
        return await self.execute_run(run_id, approver=approver)

    def run_turn_sync(
        self,
        user_message: str,
        recent_messages: list[dict[str, str]] | None = None,
        approver: Approver | None = None,
        *,
        session_id: str | None = None,
        acceptance: AcceptanceSpec | None = None,
        reviewer: str = "default",
        task_mode: TaskMode = "develop",
        review_target: ReviewTarget | None = None,
    ) -> AgentFinal:
        return asyncio.run(
            self.run_turn(
                user_message,
                recent_messages=recent_messages,
                approver=approver,
                session_id=session_id,
                acceptance=acceptance,
                reviewer=reviewer,
                task_mode=task_mode,
                review_target=review_target,
            )
        )

    def _previous_verified_run(self, session_id: str, current_run_id: str) -> str | None:
        return previous_verified_run(self.task_store, self.workspace_manager, session_id, current_run_id)


def previous_verified_run(store: TaskStore, manager: WorkspaceManager, session_id: str,
                          current_run_id: str) -> str | None:
    runs = store.list_runs(session_id)
    current_index = next((i for i, item in enumerate(runs) if item.run_id == current_run_id), len(runs))
    for run in reversed(runs[:current_index]):
        if run.task_mode != "develop" or run.status != RunStatus.SUCCEEDED:
            continue
        if (run.verification or {}).get("status") in {"passed", "not_applicable"}:
            workspace = manager.get_run(run.run_id)
            if workspace is not None and workspace.source_root.is_dir():
                return run.run_id
    return None


def prepare_run_source(store: TaskStore, manager: WorkspaceManager, workspace_id: str,
                       run_id: str) -> RunWorkspace:
    existing = manager.get_run(run_id)
    if existing is not None and existing.source_root.is_dir():
        return existing
    run = store.get_run(run_id)
    if run is None:
        raise KeyError(f"unknown run: {run_id}")
    if (run.workspace_snapshot or {}).get("source_revision"):
        raise WorkspaceError("Prepared input snapshot is missing; refusing to rebuild from newer source.")
    parent = previous_verified_run(store, manager, run.session_id, run_id)
    prepared = manager.prepare_run(workspace_id, run_id, parent_run_id=parent)
    manifest = json.loads(prepared.manifest_path.read_text())
    store.update_run(run_id, workspace_snapshot={
        **(run.workspace_snapshot or {}), "source_run_id": parent,
        "source_revision": compute_workspace_revision(prepared.source_root),
        "baseline_commit": prepared.baseline_commit, "prepared_at": manifest["created_at"],
    })
    return prepared
