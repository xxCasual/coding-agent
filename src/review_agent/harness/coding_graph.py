from __future__ import annotations

import asyncio
import json
import shlex
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

try:
    from langgraph.types import Command, interrupt
except ImportError:  # pragma: no cover - version skew
    Command = None  # type: ignore[misc, assignment]
    interrupt = None

from review_agent.config import Settings
from review_agent.harness.acceptance import AcceptanceGate
from review_agent.harness.approval import ApprovalDecision, ApprovalPolicy
from review_agent.harness.context import ContextAssembler, FragmentCache, mark_truncated_observation
from review_agent.harness.memory import AgentMemory
from review_agent.harness.memory.models import LoadedMemories
from review_agent.harness.models import (
    AgentEvent,
    AgentFinal,
    ApprovalRequest,
    Message,
    PlanResult,
    ReplayCategory,
    RunStatus,
    ToolCall,
    ToolResult,
    VerificationRecord,
    VerificationStatus,
)
from review_agent.harness.task_store import ApprovalRecord, ArtifactRecord, TaskStore
from review_agent.harness.tools import ToolRegistry
from review_agent.harness.workspace_revision import compute_workspace_revision

Approver = Callable[[ApprovalRequest], bool]
EventSink = Callable[[AgentEvent], None]
AsyncModelComplete = Callable[..., Awaitable[Any]]


class CodingGraphState(TypedDict, total=False):
    session_id: str
    run_id: str
    requirement: str
    profile_id: str
    pending_tool_calls: list[dict[str, Any]]
    tool_cursor: int
    step_count: int
    repair_rounds: int
    workspace_revision: str
    cancel_requested: bool
    ingested_message_ids: list[str]
    phase: str
    final_message: str
    verification_status: str
    changed_files: list[str]
    failure_evidence: list[str]
    loaded_memory_count: int
    done: bool
    cursor_node: str


@dataclass
class CodingDeps:
    workspace_root: Any
    store: TaskStore
    model_complete: AsyncModelComplete
    tool_registry: ToolRegistry
    approval_policy: ApprovalPolicy
    memory: AgentMemory
    acceptance_gate: AcceptanceGate
    context_assembler: ContextAssembler
    settings: Settings
    fragment_cache: FragmentCache = field(default_factory=FragmentCache)
    approver: Approver | None = None
    event_sink: EventSink | None = None
    profile_id: str = "deepseek"
    checkpointer: Any = None
    executor: Any = None
    execution_context: Any = None
    workspace_manager: Any = None
    run_workspace: Any = None
    skill_loader: Any = None
    mcp_manager: Any = None


class CodingGraphApp:
    """Official LangGraph coding runner. Node methods stay serializable-state only."""

    def __init__(self, deps: CodingDeps) -> None:
        self.deps = deps

    def compile(self):
        graph = StateGraph(CodingGraphState)
        graph.add_node("ingest", self._ingest)
        graph.add_node("model_decide", self._model_decide)
        graph.add_node("gate_tool", self._gate_tool)
        graph.add_node("accept_or_continue", self._accept_or_continue)
        graph.add_edge(START, "ingest")
        graph.add_conditional_edges("ingest", _route_after("ingest"), _ROUTE_MAP)
        graph.add_conditional_edges("model_decide", _route_after("model_decide"), _ROUTE_MAP)
        graph.add_conditional_edges("gate_tool", _route_after("gate_tool"), _ROUTE_MAP)
        graph.add_conditional_edges("accept_or_continue", _route_after("accept_or_continue"), _ROUTE_MAP)
        return graph.compile(checkpointer=self.deps.checkpointer)

    async def ainvoke(
        self,
        initial: CodingGraphState,
        *,
        thread_id: str,
        resume: Any = None,
    ) -> CodingGraphState:
        compiled = self.compile()
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 200}
        payload: Any = initial
        snapshot = None
        try:
            snapshot = await compiled.aget_state(config)
        except Exception:
            snapshot = None
        has_checkpoint = bool(
            snapshot is not None
            and (
                getattr(snapshot, "values", None)
                or getattr(snapshot, "next", None)
                or getattr(snapshot, "tasks", None)
            )
        )
        if has_checkpoint:
            if Command is not None:
                payload = Command(resume=resume if resume is not None else True)
            else:
                payload = None
        try:
            result = await compiled.ainvoke(payload, config=config)
        except Exception as exc:
            if type(exc).__name__ == "GraphInterrupt":
                return _paused_state(initial, snapshot)
            raise
        if isinstance(result, dict) and result.get("__interrupt__"):
            return result  # type: ignore[return-value]
        interrupts = getattr(result, "__interrupt__", None) if result is not None else None
        if interrupts:
            return result if isinstance(result, dict) else _paused_state(initial, snapshot)
        return result  # type: ignore[return-value]

    async def _ingest(self, state: CodingGraphState) -> dict[str, Any]:
        deps = self.deps
        run_id = state["run_id"]
        run = deps.store.get_run(run_id)
        if run is None:
            return {"done": True, "final_message": "unknown run", "phase": "done"}
        if run.cancel_requested or state.get("cancel_requested"):
            if _cancel_with_unknown(deps, run_id):
                return {
                    "done": True,
                    "cancel_requested": True,
                    "final_message": "needs_attention",
                    "phase": "done",
                }
            deps.store.update_run(run_id, status=RunStatus.CANCELLED, cancel_requested=True)
            _emit(deps, run_id, "run.cancelled", "Cancel requested.")
            return {
                "done": True,
                "cancel_requested": True,
                "final_message": "cancelled",
                "phase": "done",
            }

        pending = deps.store.drain_pending(run_id)
        ingested = list(state.get("ingested_message_ids") or [])
        remaining_tools = list(state.get("pending_tool_calls") or [])[state.get("tool_cursor", 0) :]
        if pending and remaining_tools:
            for raw in remaining_tools:
                tool_call = _tool_call_from_dict(raw)
                result = ToolResult(
                    success=False,
                    summary="Skipped because a newer user message superseded this tool batch.",
                    call_id=tool_call.call_id,
                    error_code="skipped_superseded",
                )
                _persist_tool_result(deps, state, tool_call, result)
                _emit(
                    deps,
                    run_id,
                    "tool.finished",
                    result.summary,
                    tool_call=tool_call,
                    tool_result=result,
                )
            remaining_tools = []

        for message in pending:
            if message.message_id in ingested:
                continue
            ingested.append(message.message_id)
            _emit(
                deps,
                run_id,
                "message.ingested",
                f"Ingested message {message.message_id}",
                payload={"message_id": message.message_id, "role": message.role},
            )
        revision = compute_workspace_revision(deps.workspace_root)
        deps.store.update_run(run_id, workspace_revision=revision, status=RunStatus.RUNNING)
        return {
            "ingested_message_ids": ingested,
            "pending_tool_calls": remaining_tools,
            "tool_cursor": 0,
            "workspace_revision": revision,
            "phase": "model" if not remaining_tools else "tool",
            "cancel_requested": False,
        }

    async def _model_decide(self, state: CodingGraphState) -> dict[str, Any]:
        deps = self.deps
        run_id = state["run_id"]
        session_id = state["session_id"]
        run = deps.store.get_run(run_id)
        assert run is not None
        if run.cancel_requested:
            return {"cancel_requested": True, "phase": "ingest"}

        step_count = int(state.get("step_count") or 0)
        if step_count >= deps.settings.review_agent_agent_max_steps:
            msg = "Reached max agent steps before a final answer."
            deps.store.update_run(run_id, status=RunStatus.FAILED)
            _emit(deps, run_id, "run.failed", msg)
            return {"done": True, "final_message": msg, "phase": "done"}

        history = deps.store.list_messages(session_id)
        loaded = deps.memory.load_for_turn(state["requirement"])
        tool_specs = [
            {
                "name": spec.name,
                "description": spec.description,
                "risk": spec.risk.value,
                "replay_category": spec.replay_category.value,
                "input_schema": spec.input_schema,
            }
            for spec in deps.tool_registry.specs()
        ]
        try:
            profile = deps.settings.get_model_profile(state.get("profile_id") or deps.profile_id)
        except KeyError:
            profile = None
        assembled = deps.context_assembler.assemble(
            history=history,
            requirement=state["requirement"],
            loaded_memory=loaded if isinstance(loaded, LoadedMemories) else LoadedMemories("", []),
            tool_specs=tool_specs,
            profile=profile,
            skill_hints=_skill_hint_lines(deps),
            failure_evidence=list(state.get("failure_evidence") or []),
            changed_files=list(state.get("changed_files") or []),
        )
        mode_instruction = (
            f"Task mode is frozen as {run.task_mode}. Source: "
            + json.dumps(run.workspace_snapshot or {}, ensure_ascii=False)
            + "\nPrior plans/reviews are evidence from their recorded version, not permission to change this mode."
            + f"\nActive run_id: {run_id}\nCurrent workspace_revision: {compute_workspace_revision(deps.workspace_root)}"
            + "\nUse these current identifiers when delegating review. The source snapshot is the input version; "
            "it is not the current version after edits. Omit target.baseline and target.workspace_revision "
            "unless explicitly constraining them; the review service resolves them."
        )
        from review_agent.harness.reviewer import reviewer_is_off

        if run.task_mode == "develop":
            if reviewer_is_off(run):
                mode_instruction += "\nReviewer is disabled. Implement, test and deliver without delegating review."
            else:
                mode_instruction += (
                    "\nrun_changes means changes made by this run; workspace_changes means the original input "
                    "changes before this run. For develop delivery, use the default run_changes target with "
                    "no custom focus; scoped/focused reviews do not replace the required delivery review."
                    " Once implementation and tests are ready, return a final summary to start the runtime's "
                    "frozen acceptance checks and delivery review. You will receive any findings for handling. "
                    "Do not spend review calls before those checks. Record a reasoned not_adopted disposition "
                    "for unsupported claims or optional style suggestions rather than making unrelated edits."
                )
        if run.task_mode == "plan":
            mode_instruction += (
                "\nRead files and plan only. Do not execute scripts or change code. Return a JSON object "
                "with files (paths), steps (nonempty), validation (nonempty proposed checks), "
                "uncertainties (may be empty); each field is a list of strings. These checks are planned, not run."
            )
        elif run.task_mode == "review":
            mode_instruction += (
                "\nRead-only review. Do not fix findings or run commands. A structured Reviewer report for "
                "the frozen target is required at completion: "
                + (run.review_target.model_dump_json() if run.review_target else "workspace_changes")
                + f". For delegate_review use run_id={run.run_id} and the current workspace revision."
            )
        try:
            turn = await deps.model_complete(
                [*assembled.messages, Message(role="system", content=mode_instruction, run_id=run_id)],
                tools=deps.tool_registry.specs(),
                profile_id=state.get("profile_id") or deps.profile_id,
            )
        except TimeoutError:
            deps.store.update_run(run_id, status=RunStatus.INTERRUPTED)
            _emit(deps, run_id, "run.interrupted", "Model timed out; resume to retry.")
            _graph_interrupt({"reason": "model_timeout", "run_id": run_id})
            turn = await deps.model_complete(
                assembled.messages,
                tools=deps.tool_registry.specs(),
                profile_id=state.get("profile_id") or deps.profile_id,
            )
        except asyncio.CancelledError:
            return {"cancel_requested": True, "phase": "ingest"}
        step_count += 1
        usage_obj = getattr(turn, "usage", None)
        incoming = None
        if usage_obj is not None:
            incoming = (
                usage_obj.model_dump(mode="json")
                if hasattr(usage_obj, "model_dump")
                else {
                    "profile_id": getattr(usage_obj, "profile_id", None),
                    "input_tokens": getattr(usage_obj, "input_tokens", None),
                    "output_tokens": getattr(usage_obj, "output_tokens", None),
                    "model_latency_ms": getattr(usage_obj, "model_latency_ms", None),
                    "estimated_cost": getattr(usage_obj, "estimated_cost", None),
                    "price_config_id": getattr(usage_obj, "price_config_id", None),
                    "price_as_of": getattr(usage_obj, "price_as_of", None),
                }
            )
        from review_agent.db.codec import merge_usage

        current = deps.store.get_run(run_id)
        budget = dict((current.budget if current else None) or {})
        budget["max_steps"] = deps.settings.review_agent_agent_max_steps
        budget.setdefault("reviewer_max_steps", deps.settings.review_agent_reviewer_max_steps)
        budget["step_count"] = step_count
        deps.store.update_run(
            run_id,
            usage=merge_usage(current.usage if current else None, incoming),
            budget=budget,
        )
        if getattr(turn, "raw_error", None):
            err = str(turn.raw_error).lower()
            error_code = getattr(turn, "error_code", None)
            if "timeout" in err:
                deps.store.update_run(run_id, status=RunStatus.INTERRUPTED)
                _emit(deps, run_id, "run.interrupted", f"Model timed out: {turn.raw_error}")
                _graph_interrupt({"reason": "model_timeout", "run_id": run_id, "error": turn.raw_error})
            msg = f"Model error: {turn.raw_error}"
            deps.store.update_run(run_id, status=RunStatus.FAILED)
            _emit(
                deps,
                run_id,
                "model.error",
                msg,
                payload={"error_code": error_code or "stream_failed"},
            )
            _emit(
                deps,
                run_id,
                "run.failed",
                msg,
                payload={"error_code": error_code or "stream_failed"},
            )
            return {
                "done": True,
                "final_message": msg,
                "step_count": step_count,
                "phase": "done",
                "loaded_memory_count": len(loaded.entries),
            }

        content = getattr(turn, "content", "") or ""
        tool_calls = list(getattr(turn, "tool_calls", None) or [])
        if content:
            _emit(deps, run_id, "model.text_delta", content[:500], payload={"text": content})
        _emit(deps, run_id, "model.done", "Model turn complete.")

        if tool_calls:
            normalized: list[ToolCall] = []
            for tool_call in tool_calls:
                call_id = tool_call.call_id or str(uuid.uuid4())
                normalized.append(
                    ToolCall(
                        name=tool_call.name,
                        arguments=dict(tool_call.arguments or {}),
                        call_id=call_id,
                        provider_call_id=tool_call.provider_call_id or call_id,
                    )
                )
            assistant = Message(
                role="assistant",
                content=content,
                message_id=str(uuid.uuid4()),
                session_id=session_id,
                run_id=run_id,
                tool_calls=normalized,
            )
            deps.store.append_message(assistant)
            for tool_call in normalized:
                tool = deps.tool_registry.get(tool_call.name)
                replay = tool.spec.replay_category.value if tool is not None else None
                record = deps.store.record_tool_execution(
                    run_id=run_id,
                    session_id=session_id,
                    tool_call=tool_call,
                    replay_category=replay,
                )
                if record.result is None and record.execution_status is None:
                    deps.store.complete_tool_execution(tool_call.call_id, None, execution_meta={"status": "pending"})
            return {
                "pending_tool_calls": [_tool_call_to_dict(tc) for tc in normalized],
                "tool_cursor": 0,
                "step_count": step_count,
                "phase": "tool",
                "loaded_memory_count": len(loaded.entries),
            }

        assistant = Message(
            role="assistant",
            content=content or "",
            message_id=str(uuid.uuid4()),
            session_id=session_id,
            run_id=run_id,
        )
        deps.store.append_message(assistant)
        return {
            "pending_tool_calls": [],
            "tool_cursor": 0,
            "step_count": step_count,
            "final_message": content or "",
            "phase": "accept",
            "loaded_memory_count": len(loaded.entries),
        }

    async def _gate_tool(self, state: CodingGraphState) -> dict[str, Any]:
        deps = self.deps
        run_id = state["run_id"]
        run = deps.store.get_run(run_id)
        assert run is not None
        if run.cancel_requested:
            if deps.executor is not None:
                deps.executor.cancel_run(run_id)
            if deps.mcp_manager is not None:
                await deps.mcp_manager.close()
            if _cancel_with_unknown(deps, run_id):
                return {
                    "done": True,
                    "final_message": "needs_attention",
                    "phase": "done",
                    "pending_tool_calls": list(state.get("pending_tool_calls") or []),
                }
            remaining = list(state.get("pending_tool_calls") or [])[state.get("tool_cursor", 0) :]
            for raw in remaining:
                tool_call = _tool_call_from_dict(raw)
                result = ToolResult(
                    success=False,
                    summary="Cancelled before execution.",
                    call_id=tool_call.call_id,
                    error_code="cancelled",
                )
                _persist_tool_result(deps, state, tool_call, result)
            deps.store.update_run(run_id, status=RunStatus.CANCELLED)
            _emit(deps, run_id, "run.cancelled", "Cancel requested during tools.")
            return {
                "done": True,
                "final_message": "cancelled",
                "phase": "done",
                "pending_tool_calls": [],
            }

        pending = deps.store.drain_pending(run_id)
        ingested = list(state.get("ingested_message_ids") or [])
        if pending:
            remaining = list(state.get("pending_tool_calls") or [])[state.get("tool_cursor", 0) :]
            for raw in remaining:
                tool_call = _tool_call_from_dict(raw)
                result = ToolResult(
                    success=False,
                    summary="Skipped because a newer user message superseded this tool batch.",
                    call_id=tool_call.call_id,
                    error_code="skipped_superseded",
                )
                _persist_tool_result(deps, state, tool_call, result)
                _emit(
                    deps,
                    run_id,
                    "tool.finished",
                    result.summary,
                    tool_call=tool_call,
                    tool_result=result,
                )
            for message in pending:
                if message.message_id not in ingested:
                    ingested.append(message.message_id)
                    _emit(
                        deps,
                        run_id,
                        "message.ingested",
                        f"Ingested message {message.message_id}",
                        payload={"message_id": message.message_id},
                    )
            return {
                "ingested_message_ids": ingested,
                "pending_tool_calls": [],
                "tool_cursor": 0,
                "phase": "model",
            }

        cursor = int(state.get("tool_cursor") or 0)
        pending_tools = list(state.get("pending_tool_calls") or [])
        if cursor >= len(pending_tools):
            return {"phase": "model", "pending_tool_calls": [], "tool_cursor": 0}

        tool_call = _tool_call_from_dict(pending_tools[cursor])
        _emit(deps, run_id, "tool.started", f"Starting {tool_call.name}", tool_call=tool_call)
        result, approval_request = await _execute_one_tool(deps, tool_call, run_id=run_id)
        if result.error_code == "approval_pending":
            return {
                "phase": "waiting_approval",
                "done": False,
                "tool_cursor": cursor,
                "pending_tool_calls": pending_tools,
            }
        if approval_request is not None and result.error_code == "approval_blocked":
            _emit(
                deps,
                run_id,
                "approval.blocked",
                result.summary,
                tool_call=tool_call,
                tool_result=result,
                approval_request=approval_request,
            )
        elif approval_request is not None and result.error_code == "approval_rejected":
            _emit(
                deps,
                run_id,
                "approval.rejected",
                result.summary,
                tool_call=tool_call,
                tool_result=result,
                approval_request=approval_request,
            )

        event_type = "tool.finished"
        if result.error_code in {"unknown_tool", "invalid_arguments", "execution_error"}:
            event_type = "tool.failed"
        _emit(
            deps,
            run_id,
            event_type,
            result.summary,
            tool_call=tool_call,
            tool_result=result,
        )
        _flush_skill_events(deps, run_id, tool_call)
        _persist_tool_result(deps, state, tool_call, result)

        changed = list(state.get("changed_files") or [])
        for path in result.changed_files:
            if path not in changed:
                changed.append(path)
            deps.fragment_cache.invalidate(deps.workspace_root / path)
        failure_evidence = list(state.get("failure_evidence") or [])
        if not result.success:
            failure_evidence.append(result.summary[:300])

        next_cursor = cursor + 1
        phase: Literal["tool", "model"] = "tool" if next_cursor < len(pending_tools) else "model"
        return {
            "tool_cursor": next_cursor if phase == "tool" else 0,
            "pending_tool_calls": pending_tools if phase == "tool" else [],
            "phase": phase,
            "changed_files": changed,
            "failure_evidence": failure_evidence[-8:],
            "workspace_revision": compute_workspace_revision(deps.workspace_root),
        }

    async def _accept_or_continue(self, state: CodingGraphState) -> dict[str, Any]:
        deps = self.deps
        run_id = state["run_id"]
        run = deps.store.get_run(run_id)
        assert run is not None
        if run.task_mode != "develop":
            return await _finish_read_only(deps, state, run)
        outcome = deps.acceptance_gate.evaluate(run.acceptance)
        status = outcome.record.status
        _emit(
            deps,
            run_id,
            "verification.completed",
            f"verification={status.value}",
            payload={
                "status": status.value,
                "workspace_revision": outcome.record.workspace_revision,
                "details": outcome.details,
            },
        )
        deps.store.update_run(
            run_id,
            workspace_revision=outcome.record.workspace_revision,
            verification={
                "status": status.value,
                "command": outcome.record.command,
                "exit_code": outcome.record.exit_code,
                "workspace_revision": outcome.record.workspace_revision,
                "evidence_refs": list(outcome.record.evidence_refs),
            },
        )

        verification_ok = (
            status == VerificationStatus.NOT_APPLICABLE
            or outcome.passed
            or (status == VerificationStatus.UNVERIFIED and run.acceptance.mode != "commands")
        )
        if verification_ok:
            review_gate = await _apply_review_gate(self.deps, state, run, outcome.record.workspace_revision)
            if review_gate is not None:
                return review_gate
            final = state.get("final_message") or (
                "Done."
                if status != VerificationStatus.UNVERIFIED or run.acceptance.mode == "commands"
                else "Completed without executable acceptance."
            )
            if status == VerificationStatus.UNVERIFIED and run.acceptance.mode != "commands":
                final = state.get("final_message") or "Completed without executable acceptance."
            elif not (status == VerificationStatus.NOT_APPLICABLE or outcome.passed):
                final = state.get("final_message") or "Completed without executable acceptance."
            else:
                final = state.get("final_message") or "Done."
            if deps.workspace_manager is not None and deps.run_workspace is not None:
                from review_agent.db.codec import file_sha256

                patch = deps.workspace_manager.compute_delivery_patch(run_id)
                body = patch.read_bytes()
                deps.store.register_artifact(ArtifactRecord(
                    artifact_id=str(uuid.uuid4()), run_id=run_id, kind="patch", summary=patch.name,
                    size_bytes=len(body), content_hash=file_sha256(body),
                    storage_path=patch.relative_to(deps.workspace_manager.data_root).as_posix(),
                ))
            deps.store.update_run(run_id, status=RunStatus.SUCCEEDED)
            deps.memory.extract_after_turn(
                state["requirement"], final, list(state.get("failure_evidence") or [])
            )
            deps.memory.compact_if_needed()
            _emit(deps, run_id, "run.succeeded", final)
            return {
                "done": True,
                "phase": "done",
                "verification_status": status.value,
                "final_message": final,
            }

        repair_rounds = int(state.get("repair_rounds") or 0)
        max_repairs = deps.settings.review_agent_max_verification_repairs
        if repair_rounds >= max_repairs:
            final = state.get("final_message") or (
                f"Acceptance failed after {max_repairs} repair rounds."
            )
            deps.store.update_run(run_id, status=RunStatus.FAILED)
            _emit(deps, run_id, "run.failed", final)
            return {
                "done": True,
                "phase": "done",
                "verification_status": status.value,
                "final_message": final,
                "repair_rounds": repair_rounds,
            }

        evidence = list(state.get("failure_evidence") or [])
        evidence.extend(outcome.details)
        repair_msg = Message(
            role="user",
            content=(
                "Acceptance checks failed. Continue fixing; do not claim success until checks pass.\n"
                + "\n".join(outcome.details)
            ),
            message_id=str(uuid.uuid4()),
            session_id=state["session_id"],
            run_id=run_id,
        )
        deps.store.append_message(repair_msg)
        _emit(deps, run_id, "verification.repair", f"Starting repair round {repair_rounds + 1}")
        return {
            "done": False,
            "phase": "model",
            "repair_rounds": repair_rounds + 1,
            "failure_evidence": evidence[-12:],
            "verification_status": status.value,
            "final_message": "",
        }



def _next_node(current: str, state: CodingGraphState) -> str:
    phase = state.get("phase")
    if current == "ingest":
        return "gate_tool" if phase == "tool" else "model_decide"
    if current == "model_decide":
        if phase == "tool":
            return "gate_tool"
        if phase == "accept":
            return "accept_or_continue"
        return "ingest"
    if current == "gate_tool":
        return "gate_tool" if phase == "tool" else "ingest"
    if current == "accept_or_continue":
        return "ingest"
    return "ingest"


_ROUTE_MAP = {
    "ingest": "ingest",
    "model_decide": "model_decide",
    "gate_tool": "gate_tool",
    "accept_or_continue": "accept_or_continue",
    "end": END,
}


def _route_after(current: str):
    def route(state: CodingGraphState) -> str:
        if state.get("done"):
            return "end"
        if state.get("phase") == "waiting_approval":
            return "end"
        return _next_node(current, state)

    return route


def build_coding_graph(deps: CodingDeps) -> CodingGraphApp:
    return CodingGraphApp(deps)


async def run_coding_graph(
    deps: CodingDeps,
    *,
    session_id: str,
    run_id: str,
    requirement: str,
    profile_id: str = "deepseek",
    resume: Any = None,
) -> AgentFinal:
    if deps.checkpointer is None:
        from langgraph.checkpoint.memory import InMemorySaver

        deps.checkpointer = InMemorySaver()
    app = build_coding_graph(deps)
    initial: CodingGraphState = {
        "session_id": session_id,
        "run_id": run_id,
        "requirement": requirement,
        "profile_id": profile_id,
        "pending_tool_calls": [],
        "tool_cursor": 0,
        "step_count": 0,
        "repair_rounds": 0,
        "workspace_revision": compute_workspace_revision(deps.workspace_root),
        "cancel_requested": False,
        "ingested_message_ids": [],
        "phase": "ingest",
        "final_message": "",
        "verification_status": "",
        "changed_files": [],
        "failure_evidence": [],
        "loaded_memory_count": 0,
        "done": False,
    }
    final_state = await app.ainvoke(initial, thread_id=run_id, resume=resume)
    events = deps.store.list_events(run_id)
    run = deps.store.get_run(run_id)
    verification = None
    if run and run.verification:
        verification = VerificationRecord(
            status=VerificationStatus(run.verification["status"]),
            command=run.verification.get("command"),
            exit_code=run.verification.get("exit_code"),
            workspace_revision=run.verification.get("workspace_revision"),
            evidence_refs=list(run.verification.get("evidence_refs") or []),
        )
    paused = False
    if isinstance(final_state, dict):
        paused = bool(final_state.get("__interrupt__")) or final_state.get("phase") == "waiting_approval"
    if run and run.status in {
        RunStatus.WAITING_APPROVAL,
        RunStatus.INTERRUPTED,
        RunStatus.NEEDS_ATTENTION,
    }:
        paused = True
    return AgentFinal(
        message=str((final_state or {}).get("final_message") or ""),
        events=events,
        loaded_memory_count=int((final_state or {}).get("loaded_memory_count") or 0),
        session_id=session_id,
        run_id=run_id,
        verification=verification,
        run_status=(run.status.value if run else ("interrupted" if paused else "")),
    )


async def _finish_read_only(deps: CodingDeps, state: CodingGraphState, run) -> dict[str, Any]:
    run_id = run.run_id
    revision = compute_workspace_revision(deps.workspace_root)
    source_revision = (run.workspace_snapshot or {}).get("source_revision")
    if source_revision and revision != source_revision:
        final = "Read-only source changed; task cannot be completed against its original snapshot."
        deps.store.update_run(run_id, status=RunStatus.FAILED)
        _emit(deps, run_id, "run.failed", final)
        return {"done": True, "phase": "done", "final_message": final}
    verification = {"status": "not_applicable", "workspace_revision": revision,
                    "command": None, "exit_code": None, "evidence_refs": []}
    deps.store.update_run(run_id, workspace_revision=revision, verification=verification)
    _emit(deps, run_id, "verification.completed", "Code acceptance is not applicable to read-only tasks.",
          payload=verification)
    if run.task_mode == "review":
        try:
            gate = await _apply_review_gate(deps, state, deps.store.get_run(run_id), revision)
        except (ValueError, OSError) as exc:
            from review_agent.harness.reviewer import ReviewResult
            report = ReviewResult(status="unavailable", workspace_revision=revision,
                                  target=run.review_target, warnings=[str(exc)])
            deps.store.update_run(run_id, review=report.as_review_dict())
            return _interrupt_for_failed_review(deps, run_id, report.as_review_dict())
        if gate is not None:
            return gate
        report = deps.store.get_run(run_id).review or {}
        findings = report.get("findings") or []
        target = report.get("target") or {}
        scope = {"paths": "Selected source paths", "workspace_changes": "Original workspace changes",
                 "run_changes": "Current task changes"}.get(target.get("kind"), "Unknown scope")
        final = (f"Review completed: {len(findings)} finding(s). Code acceptance: not applicable.\n"
                 f"Target: {scope}\nPaths: {', '.join(target.get('paths') or []) or 'All within target'}\n"
                 f"Revision: {target.get('workspace_revision') or 'unknown'}")
        if target.get("focus"):
            final += f"\nFocus: {target['focus']}"
        for finding in findings:
            final += (f"\n- {finding['file_path']}:{finding['start_line']} {finding['title']}"
                      f"\n  {finding['evidence']}")
        if report.get("unchecked"):
            final += "\nUnchecked: " + "; ".join(report["unchecked"])
    else:
        from review_agent.reviewers.finding_parser import parse_json_object
        try:
            plan = PlanResult.model_validate(parse_json_object(state.get("final_message") or ""))
        except (ValueError, TypeError):
            final = "Plan unavailable: expected files, steps, validation and uncertainties lists."
            deps.store.update_run(run_id, status=RunStatus.FAILED)
            _emit(deps, run_id, "run.failed", final)
            return {"done": True, "phase": "done", "final_message": final, "verification_status": "not_applicable"}
        final = f"Plan completed for source revision {revision}. Proposed checks have not been run."
        for heading, items in plan.model_dump().items():
            final += "\n" + heading + ":\n" + "\n".join(f"- {item}" for item in items)
    if compute_workspace_revision(deps.workspace_root) != revision:
        final = "Read-only source changed while preparing the report."
        deps.store.update_run(run_id, status=RunStatus.FAILED)
        _emit(deps, run_id, "run.failed", final)
        return {"done": True, "phase": "done", "final_message": final, "verification_status": "not_applicable"}
    deps.store.append_message(Message(role="assistant", content=final, message_id=str(uuid.uuid4()),
                                     session_id=run.session_id, run_id=run_id))
    deps.store.update_run(run_id, status=RunStatus.SUCCEEDED)
    _emit(deps, run_id, "run.succeeded", final, payload={"task_mode": run.task_mode})
    return {"done": True, "phase": "done", "final_message": final, "verification_status": "not_applicable"}


async def _apply_review_gate(
    deps: CodingDeps,
    state: CodingGraphState,
    run,
    workspace_revision: str | None,
) -> dict[str, Any] | None:
    """Return a graph update if delivery must wait on Reviewer; None to allow success."""
    from review_agent.harness.models import ToolCall
    from review_agent.harness.reviewer import (
        dispositions_complete,
        prepare_review,
        ReviewResult,
        forced_review_call_id,
        initial_review_state,
        review_is_current,
        review_required_for_delivery,
        review_tool_result,
        reviewer_is_off,
        run_delegated_review,
    )

    run_id = state["run_id"]
    run = deps.store.get_run(run_id) or run
    if run.review is None:
        deps.store.update_run(run_id, review=initial_review_state("default"))
        run = deps.store.get_run(run_id) or run
    review_task = run.task_mode == "review"
    if reviewer_is_off(run) and not review_task:
        snapshot = dict(run.review or initial_review_state("off"))
        snapshot["mode"] = "off"
        snapshot["enabled"] = False
        deps.store.update_run(run_id, review=snapshot)
        return None
    changed = list(state.get("changed_files") or [])
    revision = workspace_revision or compute_workspace_revision(deps.workspace_root)
    if not review_task and not review_required_for_delivery(run, changed):
        return None
    _, _, _, cache_key = prepare_review(deps.workspace_root, run=run, store=deps.store,
        settings=deps.settings, profile_id=state.get("profile_id") or deps.profile_id,
        target=run.review_target if review_task else None)
    def current_review(review):
        if review_task:
            return bool(review and review.get("status") == "completed" and review.get("cache_key") == cache_key)
        return review_is_current(review, revision, cache_key)

    if current_review(run.review) and (review_task or dispositions_complete(run.review)):
        return None
    if current_review(run.review) and not review_task and not dispositions_complete(run.review):
        return _review_continue(
            deps,
            state,
            "Review findings need a disposition (fixed or not_adopted) before delivery.",
            run.review or {},
        )

    call_id = forced_review_call_id(run_id, revision, cache_key)
    existing = deps.store.get_tool_execution(call_id)
    if existing is not None and existing.result is not None:
        run = deps.store.get_run(run_id) or run
        if not current_review(run.review):
            recorded = ReviewResult.model_validate_json(existing.result.stdout)
            deps.store.update_run(run_id, review=recorded.as_review_dict(mode=(run.review or {}).get("mode", "default")))
            run = deps.store.get_run(run_id) or run
        if current_review(run.review) and (review_task or dispositions_complete(run.review)):
            return None
        if run.review and run.review.get("status") == "completed":
            return _review_continue(
                deps,
                state,
                "Review findings need a disposition (fixed or not_adopted) before delivery.",
                run.review,
            )
        return _interrupt_for_failed_review(deps, run_id, run.review or {})

    subtask_id = (run.review or {}).get("subtask_id") or f"review-{run_id[:8]}"
    tool_call = ToolCall(
        name="delegate_review",
        arguments={"run_id": run_id, "subtask_id": subtask_id, "workspace_revision": revision,
                   **({"target": run.review_target.model_dump(mode="json")} if review_task and run.review_target else {})},
        call_id=call_id,
        provider_call_id=call_id,
    )
    deps.store.record_tool_execution(
        run_id=run_id,
        session_id=state["session_id"],
        tool_call=tool_call,
        replay_category=ReplayCategory.REPEATABLE_READ.value,
    )

    def emit(event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
        _emit(
            deps,
            run_id,
            event_type,
            message,
            tool_call=tool_call,
            payload=payload or {},
        )

    result = await run_delegated_review(
        deps.workspace_root,
        store=deps.store,
        model_complete=deps.model_complete,
        settings=deps.settings,
        profile_id=state.get("profile_id") or deps.profile_id,
        run=run,
        subtask_id=subtask_id,
        workspace_revision=revision,
        emit=emit,
        target=run.review_target if review_task else None,
        extra_started={"call_id": call_id, "run_id": run_id},
        extra_completed={"call_id": call_id},
    )
    tool_result = review_tool_result(result, call_id=call_id)
    deps.store.complete_tool_execution(call_id, tool_result)
    if result.status != "completed":
        return _interrupt_for_failed_review(deps, run_id, result.as_review_dict())
    if not review_task and result.findings and not dispositions_complete(deps.store.get_run(run_id).review if deps.store.get_run(run_id) else None):
        return _review_continue(
            deps,
            state,
            "Reviewer reported findings. Record a disposition for each, then re-verify after any code change.",
            result.as_review_dict(),
        )
    return None


def _review_continue(deps: CodingDeps, state: CodingGraphState, message: str, review: dict[str, Any]) -> dict[str, Any]:
    run_id = state["run_id"]
    import json

    findings = review.get("findings") or []
    body = message + "\n" + json.dumps(findings, ensure_ascii=False)[:4000]
    repair_msg = Message(
        role="user",
        content=body,
        message_id=str(uuid.uuid4()),
        session_id=state["session_id"],
        run_id=run_id,
    )
    deps.store.append_message(repair_msg)
    return {
        "done": False,
        "phase": "model",
        "final_message": "",
        "verification_status": state.get("verification_status") or "",
    }


def _interrupt_for_failed_review(deps: CodingDeps, run_id: str, review: dict[str, Any]) -> dict[str, Any]:
    status = str(review.get("status") or "failed")
    msg = f"Required review did not complete ({status}); code and evidence were kept."
    deps.store.update_run(run_id, status=RunStatus.INTERRUPTED)
    _emit(deps, run_id, "run.interrupted", msg, payload={"review_status": status, "subtask_id": review.get("subtask_id")})
    return {
        "done": True,
        "phase": "done",
        "final_message": msg,
    }


def _emit(
    deps: CodingDeps,
    run_id: str,
    event_type: str,
    message: str,
    *,
    tool_call: ToolCall | None = None,
    tool_result: ToolResult | None = None,
    approval_request: ApprovalRequest | None = None,
    payload: dict[str, Any] | None = None,
) -> AgentEvent:
    event = deps.store.append_event(
        AgentEvent(
            type=event_type,
            message=message,
            tool_call=tool_call,
            tool_result=tool_result,
            approval_request=approval_request,
            run_id=run_id,
            payload=payload or {},
        )
    )
    if deps.event_sink is not None:
        deps.event_sink(event)
    return event


def _skill_hint_lines(deps: CodingDeps) -> list[str]:
    loader = deps.skill_loader
    if loader is None:
        return []
    try:
        hints = loader.list_hints()
    except Exception:
        return []
    return [f"- {item['name']}: {item['description']}" for item in hints]


def _flush_skill_events(deps: CodingDeps, run_id: str, tool_call: ToolCall) -> None:
    ctx = deps.execution_context
    if ctx is None:
        return
    pending = list(getattr(ctx, "pending_skill_events", []) or [])
    if not pending:
        return
    ctx.pending_skill_events = []
    for payload in pending:
        if tool_call.call_id and "call_id" not in payload:
            payload = {**payload, "call_id": tool_call.call_id}
        _emit(
            deps,
            run_id,
            "skill.loaded",
            f"Loaded skill {payload.get('name')}",
            tool_call=tool_call,
            payload=payload,
        )


def _tool_call_to_dict(tool_call: ToolCall) -> dict[str, Any]:
    return tool_call.model_dump(mode="json")


def _tool_call_from_dict(raw: dict[str, Any]) -> ToolCall:
    return ToolCall.model_validate(
        {
            "name": str(raw.get("name") or ""),
            "arguments": dict(raw.get("arguments") or {}),
            "call_id": str(raw.get("call_id") or ""),
            "provider_call_id": raw.get("provider_call_id"),
        }
    )


def _persist_tool_result(
    deps: CodingDeps,
    state: CodingGraphState,
    tool_call: ToolCall,
    result: ToolResult,
) -> None:
    existing = deps.store.get_tool_execution(tool_call.call_id)
    if existing is not None and existing.result is not None:
        return
    meta = {}
    if deps.execution_context is not None:
        meta = dict(getattr(deps.execution_context, "last_execution_meta", {}) or {})
    deps.store.complete_tool_execution(tool_call.call_id, result, execution_meta=meta or None)
    artifact_note = ""
    if result.artifact_refs:
        artifact_note = " artifacts=" + ",".join(result.artifact_refs)
    observation = mark_truncated_observation(result.observation() + artifact_note)
    provider_id = tool_call.provider_call_id or tool_call.call_id
    deps.store.append_message(
        Message(
            role="tool",
            content=observation,
            message_id=f"tool-result:{tool_call.call_id}",
            session_id=state["session_id"],
            run_id=state["run_id"],
            provider_call_id=provider_id,
        )
    )


def _adapt_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    adapted = dict(arguments)
    if name == "run_command" and "argv" not in adapted and "command" in adapted:
        command = adapted.pop("command")
        if isinstance(command, str):
            adapted["argv"] = shlex.split(command)
        elif isinstance(command, list):
            adapted["argv"] = [str(part) for part in command]
        else:
            adapted["argv"] = command
    return adapted


def _persist_approval_intent(
    deps: CodingDeps,
    *,
    run_id: str,
    tool_call: ToolCall,
    arguments: dict[str, Any],
    revision: str | None,
    patch_hash: str | None,
    approval_id: str | None = None,
) -> None:
    """Record definite tool intent before any side effect."""
    call_id = tool_call.call_id
    deps.store.create_approval(
        ApprovalRecord(
            approval_id=approval_id or f"intent:{call_id}",
            run_id=run_id,
            intent={
                "name": tool_call.name,
                "arguments": dict(arguments),
                "call_id": call_id,
                "provider_call_id": tool_call.provider_call_id,
            },
            param_summary=", ".join(sorted(arguments.keys())),
            workspace_revision=revision,
            patch_hash=patch_hash,
            call_id=call_id,
        )
    )


async def _execute_one_tool(
    deps: CodingDeps,
    tool_call: ToolCall,
    *,
    run_id: str,
) -> tuple[ToolResult, ApprovalRequest | None]:
    call_id = tool_call.call_id or str(uuid.uuid4())
    existing_exec = deps.store.get_tool_execution(call_id)
    if existing_exec is not None and existing_exec.result is not None:
        if tool_call.name == "delegate_review" or existing_exec.tool_call.name == "delegate_review":
            from review_agent.harness.tool_params import DelegateReviewParams
            try:
                same = (tool_call.name == existing_exec.tool_call.name
                    and DelegateReviewParams.model_validate(tool_call.arguments)
                    == DelegateReviewParams.model_validate(existing_exec.tool_call.arguments))
            except ValueError:
                same = False
            if not same:
                return ToolResult(success=False, summary="Review call_id already belongs to different arguments.",
                                  call_id=call_id, error_code="invalid_arguments"), None
        return existing_exec.result, None
    arguments = _adapt_tool_arguments(tool_call.name, tool_call.arguments)
    validated = deps.tool_registry.validate_arguments(
        tool_call.name,
        arguments,
        call_id=call_id,
    )
    if isinstance(validated, ToolResult):
        return validated, None

    tool, arguments = validated
    tool_call = ToolCall(
        name=tool_call.name,
        arguments=arguments,
        call_id=call_id,
        provider_call_id=tool_call.provider_call_id or call_id,
    )
    revision = compute_workspace_revision(deps.workspace_root)
    if deps.execution_context is not None:
        deps.execution_context.call_id = call_id
        deps.execution_context.workspace_revision = revision
        if getattr(deps.acceptance_gate, "run_id", None) and not deps.execution_context.run_id:
            deps.execution_context.run_id = deps.acceptance_gate.run_id

    patch_hash = None
    if tool_call.name == "apply_patch":
        import hashlib

        patch_hash = hashlib.sha256(str(arguments.get("patch") or "").encode("utf-8")).hexdigest()

    approval = deps.approval_policy.evaluate(
        tool.spec,
        arguments,
        workspace_revision=revision,
        patch_hash=patch_hash,
    )
    if approval.decision == ApprovalDecision.BLOCK:
        request = approval.to_request(tool_call.name, arguments)
        return (
            ToolResult(
                success=False,
                summary=approval.reason,
                call_id=call_id,
                error_code="approval_blocked",
                risk_level=tool.spec.risk,
            ),
            request,
        )
    if approval.decision == ApprovalDecision.CONFIRM:
        request = approval.to_request(tool_call.name, arguments)
        approval_id = f"intent:{call_id}"
        existing = deps.store.get_approval(approval_id)
        if existing is None:
            _persist_approval_intent(
                deps,
                run_id=run_id,
                tool_call=tool_call,
                arguments=arguments,
                revision=revision,
                patch_hash=patch_hash,
                approval_id=approval_id,
            )
            existing = deps.store.get_approval(approval_id)
        if existing is not None and (
            existing.workspace_revision != revision or existing.patch_hash != patch_hash
        ):
            approval_id = f"intent:{call_id}:{revision or 'none'}"
            existing = deps.store.get_approval(approval_id)
            if existing is None:
                _persist_approval_intent(
                    deps,
                    run_id=run_id,
                    tool_call=tool_call,
                    arguments=arguments,
                    revision=revision,
                    patch_hash=patch_hash,
                    approval_id=approval_id,
                )
                existing = deps.store.get_approval(approval_id)
        decision = existing.decision if existing is not None else None
        if decision is None and deps.approver is not None:
            decision = "allow" if deps.approver(request) else "deny"
            if existing is not None:
                try:
                    deps.store.set_approval_decision(existing.approval_id, decision)
                except ValueError:
                    pass
        if decision is None:
            if existing_exec is not None:
                deps.store.complete_tool_execution(call_id, None, execution_meta={"status": "awaiting_approval"})
            deps.store.update_run(run_id, status=RunStatus.WAITING_APPROVAL)
            _emit(
                deps,
                run_id,
                "approval.requested",
                request.reason,
                tool_call=tool_call,
                approval_request=request,
            )
            resumed = _graph_interrupt(
                {
                    "approval_id": existing.approval_id if existing else approval_id,
                    "call_id": call_id,
                }
            )
            if isinstance(resumed, str):
                decision = resumed
            elif existing is not None:
                refreshed = deps.store.get_approval(existing.approval_id)
                decision = refreshed.decision if refreshed else None
        if decision == "deny":
            return (
                ToolResult(
                    success=False,
                    summary="Tool execution rejected by approval policy.",
                    call_id=call_id,
                    error_code="approval_rejected",
                    risk_level=tool.spec.risk,
                ),
                request,
            )
        if decision != "allow":
            return (
                ToolResult(
                    success=False,
                    summary="Approval is still pending.",
                    call_id=call_id,
                    error_code="approval_pending",
                    risk_level=tool.spec.risk,
                ),
                request,
            )
        if existing is not None and (
            existing.workspace_revision != revision or existing.patch_hash != patch_hash
        ):
            return (
                ToolResult(
                    success=False,
                    summary="Approval is bound to a different parameter or file version.",
                    call_id=call_id,
                    error_code="approval_stale",
                    risk_level=tool.spec.risk,
                ),
                request,
            )
        if existing_exec is not None:
            deps.store.complete_tool_execution(call_id, None, execution_meta={"status": "executing"})
        result = await deps.tool_registry.execute_async(tool, arguments, call_id=call_id)
        return result, request

    if existing_exec is not None:
        deps.store.complete_tool_execution(call_id, None, execution_meta={"status": "executing"})
    result = await deps.tool_registry.execute_async(tool, arguments, call_id=call_id)
    return result, None


def _graph_interrupt(payload: dict[str, Any]) -> Any:
    if interrupt is None:
        return payload.get("decision")
    return interrupt(payload)


def _cancel_with_unknown(deps: CodingDeps, run_id: str) -> bool:
    from review_agent.services.recovery import mark_needs_attention, unknown_side_effects, ReconcileDecision

    unknown = unknown_side_effects(deps.store, run_id)
    if not unknown:
        return False
    first = unknown[0]
    mark_needs_attention(
        deps.store,
        run_id,
        ReconcileDecision(
            status="needs_attention",
            reason="Cancel requested while command side effects are unknown.",
            call_id=first.call_id,
        ),
    )
    deps.store.update_run(run_id, cancel_requested=True, status=RunStatus.NEEDS_ATTENTION)
    _emit(
        deps,
        run_id,
        "run.needs_attention",
        "Cancel requested; unknown side effects require reconciliation.",
        payload={"call_id": first.call_id},
    )
    return True


def _paused_state(initial: CodingGraphState, snapshot: Any) -> CodingGraphState:
    values = getattr(snapshot, "values", None) if snapshot is not None else None
    if isinstance(values, dict) and values:
        merged = dict(values)
        merged["phase"] = merged.get("phase") or "waiting_approval"
        return merged  # type: ignore[return-value]
    paused = dict(initial)
    paused["phase"] = "waiting_approval"
    paused["done"] = False
    return paused  # type: ignore[return-value]
