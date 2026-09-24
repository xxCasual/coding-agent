from __future__ import annotations

import inspect
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from review_agent.config import Settings, get_settings
from review_agent.harness.models import (
    RegisteredTool,
    ReplayCategory,
    ToolResult,
    ToolRisk,
    ToolSpec,
)
from review_agent.harness.tool_params import (
    ApplyPatchParams,
    DelegateReviewParams,
    GitDiffParams,
    GitStatusParams,
    ListFilesParams,
    LoadSkillParams,
    PythonAstSummaryParams,
    ReadFileParams,
    ReadSkillResourceParams,
    ReviewPrParams,
    RunCommandParams,
    RunSkillScriptParams,
    SearchTextParams,
    SubmitReviewResponseParams,
)
from review_agent.services.command_runner import CommandRunner
from review_agent.services.executor import ExecutionStatus, Executor
from review_agent.services.patch_apply import apply_patch

MAX_OUTPUT_CHARS = 6000


@dataclass
class ExecutionContext:
    """Mutable per-run context shared with tool handlers (set by coding graph)."""

    run_id: str | None = None
    call_id: str | None = None
    workspace_revision: str | None = None
    last_execution_meta: dict[str, Any] = field(default_factory=dict)
    pending_skill_events: list[dict[str, Any]] = field(default_factory=list)


class ToolRegistry:
    def __init__(
        self,
        tools: list[RegisteredTool] | None = None,
        settings: Settings | None = None,
        *,
        executor: Executor | None = None,
        execution_context: ExecutionContext | None = None,
    ) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._allowed_names: frozenset[str] | None = None
        self._settings = settings or get_settings()
        self.executor = executor
        self.execution_context = execution_context or ExecutionContext()
        for tool in tools or []:
            self.register(tool)

    def restrict_read_only(self, *, allow_review: bool = False) -> None:
        names = {"list_files", "read_file", "search_text", "git_status", "git_diff", "python_ast_summary"}
        if allow_review:
            names.add("delegate_review")
        self._allowed_names = frozenset(names)
        self._tools = {name: tool for name, tool in self._tools.items() if name in names}

    def permission_error(self, tool: RegisteredTool, call_id: str = "") -> ToolResult | None:
        if self._allowed_names is not None and (
            tool.spec.name not in self._allowed_names or self._tools.get(tool.spec.name) is not tool
        ):
            return ToolResult(success=False, summary="Tool is not permitted in this read-only task.",
                              call_id=call_id, error_code="permission_denied")
        return None

    def register(self, tool: RegisteredTool) -> None:
        name = tool.spec.name
        if self._allowed_names is not None and name not in self._allowed_names:
            return
        if name in self._tools:
            raise ValueError(f"duplicate tool name refused: {name}")
        self._tools[name] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def registered_tools(self) -> list[RegisteredTool]:
        return list(self._tools.values())

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def validate_arguments(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        call_id: str = "",
    ) -> tuple[RegisteredTool, dict[str, Any]] | ToolResult:
        tool = self.get(name)
        if tool is None:
            return ToolResult(
                success=False,
                summary=f"Unknown tool: {name}",
                call_id=call_id,
                error_code="unknown_tool",
            )
        if tool.params_model is None:
            from review_agent.harness.mcp.schema import (
                InvalidJsonArguments,
                SchemaCapabilityError,
                validate_json_arguments,
            )

            try:
                payload = validate_json_arguments(tool.spec.input_schema, arguments or {})
            except SchemaCapabilityError as exc:
                return ToolResult(
                    success=False,
                    summary=str(exc),
                    call_id=call_id,
                    error_code="capability_missing",
                    risk_level=tool.spec.risk,
                )
            except InvalidJsonArguments as exc:
                return ToolResult(
                    success=False,
                    summary=str(exc),
                    call_id=call_id,
                    error_code="invalid_arguments",
                    risk_level=tool.spec.risk,
                )
        else:
            try:
                validated = tool.params_model.model_validate(arguments or {})
            except ValidationError as exc:
                return ToolResult(
                    success=False,
                    summary=_format_validation_error(exc),
                    call_id=call_id,
                    error_code="invalid_arguments",
                    risk_level=tool.spec.risk,
                )
            payload = validated.model_dump(exclude_none=True)
        if name in {"run_command", "run_skill_script"}:
            timeout_error = self._validate_run_command_timeout(payload, tool, call_id)
            if timeout_error is not None:
                return timeout_error
        return tool, payload

    def execute(
        self,
        tool: RegisteredTool,
        arguments: dict[str, Any],
        *,
        call_id: str = "",
    ) -> ToolResult:
        denied = self.permission_error(tool, call_id)
        if denied is not None:
            return denied
        if call_id:
            self.execution_context.call_id = call_id
        try:
            result = tool.handler(arguments)
        except Exception as exc:
            return self._execution_error(exc, tool, call_id)
        if inspect.isawaitable(result):
            raise TypeError("handler is async; use execute_async")
        return self._finalize_result(result, call_id)

    async def execute_async(
        self,
        tool: RegisteredTool,
        arguments: dict[str, Any],
        *,
        call_id: str = "",
    ) -> ToolResult:
        denied = self.permission_error(tool, call_id)
        if denied is not None:
            return denied
        if call_id:
            self.execution_context.call_id = call_id
        try:
            result = tool.handler(arguments)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            return self._execution_error(exc, tool, call_id)
        return self._finalize_result(result, call_id)

    def _execution_error(self, exc: Exception, tool: RegisteredTool, call_id: str) -> ToolResult:
        return ToolResult(
            success=False,
            summary=f"{type(exc).__name__}: {exc}",
            call_id=call_id,
            error_code="execution_error",
            risk_level=tool.spec.risk,
        )

    def _finalize_result(self, result: ToolResult, call_id: str) -> ToolResult:
        if result.call_id == call_id:
            return result
        return result.model_copy(update={"call_id": call_id})

    def _validate_run_command_timeout(
        self,
        payload: dict[str, Any],
        tool: RegisteredTool,
        call_id: str,
    ) -> ToolResult | None:
        max_timeout = self._settings.review_agent_command_timeout_seconds
        timeout = payload.get("timeout")
        if timeout is None:
            payload["timeout"] = max_timeout
            return None
        if timeout > max_timeout:
            return ToolResult(
                success=False,
                summary=f"Invalid tool arguments: timeout: Input should be less than or equal to {max_timeout}",
                call_id=call_id,
                error_code="invalid_arguments",
                risk_level=tool.spec.risk,
            )
        return None


def build_default_tool_registry(
    workspace_root: str | Path,
    settings: Settings | None = None,
    review_service_factory: Callable[[], Any] | None = None,
    *,
    executor: Executor | None = None,
    execution_context: ExecutionContext | None = None,
    include_review_pr: bool = False,
    skill_loader: Any | None = None,
) -> ToolRegistry:
    """Default coding tools. review_pr is opt-in for legacy PR CLI/API only."""
    workspace = Path(workspace_root).resolve()
    settings = settings or get_settings()
    ctx = execution_context or ExecutionContext()
    exec_engine = executor or Executor(
        backend=settings.review_agent_executor_backend,
        preview_chars=settings.review_agent_command_preview_chars,
        task_image=settings.review_agent_task_image,
        docker_network=settings.review_agent_docker_network,
    )
    runner = CommandRunner(exec_engine)
    tools = [
        _tool(
            "list_files",
            "List files under a workspace-relative path.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            ListFilesParams,
            _list_files(workspace),
        ),
        _tool(
            "read_file",
            "Read a text file from the workspace.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            ReadFileParams,
            _read_file(workspace),
        ),
        _tool(
            "search_text",
            "Search for text under a workspace path.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            SearchTextParams,
            _search_text(workspace, runner, ctx),
        ),
        _tool(
            "git_status",
            "Return git status --short.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            GitStatusParams,
            _git_status(workspace, runner, ctx),
        ),
        _tool(
            "git_diff",
            "Return git diff.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            GitDiffParams,
            _git_diff(workspace, runner, ctx),
        ),
        _tool(
            "run_command",
            "Run a shell command as argv in the workspace.",
            ToolRisk.WRITE_CONFIRM,
            ReplayCategory.NON_REPLAYABLE,
            RunCommandParams,
            _run_command(workspace, settings, runner, ctx),
        ),
        _tool(
            "apply_patch",
            "Apply a unified diff or *** Begin Patch block in the workspace.",
            ToolRisk.WRITE_CONFIRM,
            ReplayCategory.PATCH_CHECKABLE,
            ApplyPatchParams,
            _apply_patch(workspace, ctx),
        ),
    ]
    if skill_loader is not None:
        tools.extend(
            [
                _tool(
                    "load_skill",
                    "Load full Skill instructions by name (progressive disclosure).",
                    ToolRisk.READ_ONLY,
                    ReplayCategory.REPEATABLE_READ,
                    LoadSkillParams,
                    _load_skill(skill_loader, ctx),
                ),
                _tool(
                    "read_skill_resource",
                    "Read a relative file under a discovered Skill root.",
                    ToolRisk.READ_ONLY,
                    ReplayCategory.REPEATABLE_READ,
                    ReadSkillResourceParams,
                    _read_skill_resource(skill_loader, ctx),
                ),
                _tool(
                    "run_skill_script",
                    "Run a script under a Skill scripts/ directory via the Executor.",
                    ToolRisk.WRITE_CONFIRM,
                    ReplayCategory.NON_REPLAYABLE,
                    RunSkillScriptParams,
                    _run_skill_script(workspace, settings, runner, skill_loader, ctx),
                ),
            ]
        )
    registry = ToolRegistry(
        tools,
        settings=settings,
        executor=exec_engine,
        execution_context=ctx,
    )
    if include_review_pr:
        factory = review_service_factory or _default_review_service
        registry.register(
            _tool(
                "review_pr",
                "Run the existing PR review workflow.",
                ToolRisk.READ_ONLY,
                ReplayCategory.REPEATABLE_READ,
                ReviewPrParams,
                _review_pr(factory),
            )
        )
    return registry


def build_reviewer_tool_registry(
    workspace_root: str | Path,
    settings: Settings | None = None,
    *,
    executor: Executor | None = None,
    execution_context: ExecutionContext | None = None,
) -> ToolRegistry:
    """Read-only tools for the independent Reviewer loop. No write, shell, or nested delegate."""
    workspace = Path(workspace_root).resolve()
    settings = settings or get_settings()
    ctx = execution_context or ExecutionContext()
    exec_engine = executor or Executor(
        backend=settings.review_agent_executor_backend,
        preview_chars=settings.review_agent_command_preview_chars,
        task_image=settings.review_agent_task_image,
        docker_network=settings.review_agent_docker_network,
    )
    runner = CommandRunner(exec_engine)
    tools = [
        _tool(
            "list_files",
            "List files under a workspace-relative path.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            ListFilesParams,
            _list_files(workspace),
        ),
        _tool(
            "read_file",
            "Read a text file from the workspace.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            ReadFileParams,
            _read_file(workspace),
        ),
        _tool(
            "search_text",
            "Search for text under a workspace path.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            SearchTextParams,
            _search_text(workspace, runner, ctx),
        ),
        _tool(
            "git_status",
            "Return git status --short.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            GitStatusParams,
            _git_status(workspace, runner, ctx),
        ),
        _tool(
            "git_diff",
            "Return git diff.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            GitDiffParams,
            _git_diff(workspace, runner, ctx),
        ),
        _tool(
            "python_ast_summary",
            "Return a Python AST summary for a workspace file.",
            ToolRisk.READ_ONLY,
            ReplayCategory.REPEATABLE_READ,
            PythonAstSummaryParams,
            _python_ast_summary(workspace),
        ),
    ]
    return ToolRegistry(
        tools,
        settings=settings,
        executor=exec_engine,
        execution_context=ctx,
    )


def attach_review_tools(
    registry: ToolRegistry,
    *,
    workspace_root: str | Path,
    store: Any,
    model_complete: Callable[..., Any],
    settings: Settings,
    emit: Callable[[str, str, dict[str, Any] | None], None],
    profile_id: str = "deepseek",
    execution_context: ExecutionContext | None = None,
) -> None:
    """Register parent-agent review tools if missing. Handlers close over store/model."""
    if registry.get("delegate_review") is None:
        registry.register(
            _tool(
                "delegate_review",
                "Delegate a local read-only code review of the current workspace revision.",
                ToolRisk.READ_ONLY,
                ReplayCategory.REPEATABLE_READ,
                DelegateReviewParams,
                _delegate_review(
                    workspace_root,
                    store=store,
                    model_complete=model_complete,
                    settings=settings,
                    emit=emit,
                    profile_id=profile_id,
                    execution_context=execution_context,
                ),
            )
        )
    if registry.get("submit_review_response") is None:
        registry.register(
            _tool(
                "submit_review_response",
                "Record fixed/not_adopted dispositions for the current Reviewer findings.",
                ToolRisk.READ_ONLY,
                ReplayCategory.NON_REPLAYABLE,
                SubmitReviewResponseParams,
                _submit_review_response(store, emit=emit, execution_context=execution_context),
            )
        )


def _python_ast_summary(workspace: Path):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        from review_agent.tools.python_analysis_tools import python_ast_summary_tool

        path = _resolve_workspace_path(workspace, arguments["path"])
        if not path.is_file():
            return ToolResult(success=False, summary=f"File not found: {arguments['path']}")
        summary = python_ast_summary_tool(path)
        return ToolResult(
            success=True,
            summary=f"AST summary for {path.relative_to(workspace).as_posix()}.",
            stdout=summary.model_dump_json(indent=2),
        )

    return handler


def _delegate_review(
    workspace_root: str | Path,
    *,
    store: Any,
    model_complete: Callable[..., Any],
    settings: Settings,
    emit: Callable[[str, str, dict[str, Any] | None], None],
    profile_id: str,
    execution_context: ExecutionContext | None = None,
):
    async def handler(arguments: dict[str, Any]) -> ToolResult:
        import json

        from review_agent.harness.reviewer import prepare_review, review_tool_result, run_delegated_review, reviewer_is_off
        from review_agent.harness.review_target import ReviewTarget
        from review_agent.harness.workspace_revision import compute_workspace_revision

        run_id = str(arguments["run_id"])
        if execution_context and execution_context.run_id and run_id != execution_context.run_id:
            return ToolResult(success=False, summary="Review must belong to the active run.", error_code="invalid_arguments")
        subtask_id = str(arguments["subtask_id"])
        requested_revision = str(arguments["workspace_revision"])
        focus = str(arguments.get("focus") or "")
        run = store.get_run(run_id)
        if run is None:
            return ToolResult(
                success=False,
                summary=f"Unknown run: {run_id}",
                error_code="execution_error",
            )
        current = compute_workspace_revision(workspace_root)
        if run.task_mode != "review" and reviewer_is_off(run):
            return ToolResult(success=False, summary="Reviewer is disabled for this run.", error_code="permission_denied")
        target = ReviewTarget.model_validate(arguments.get("target") or {})
        if run.task_mode == "review":
            target = run.review_target or ReviewTarget(kind="workspace_changes")
            focus = target.focus
        verification = run.verification or {}
        if (run.task_mode == "develop" and target.kind == "run_changes" and not target.paths
                and not target.focus and not focus and run.acceptance.mode == "commands"
                and (verification.get("status") != "passed" or verification.get("workspace_revision") != current)):
            return ToolResult(success=False, error_code="verification_required",
                summary="Delivery review awaits the frozen acceptance checks for the current code. "
                        "Return your final implementation summary now; the runtime will run acceptance, "
                        "then invoke Reviewer and return any findings to you. Do not retry delegation before that.")
        resolved, _, _, cache_key = prepare_review(workspace_root, run=run, store=store,
            settings=settings, profile_id=profile_id, target=target, focus=focus)
        if (run.review and run.review.get("status") == "completed"
                and run.review.get("cache_key") == cache_key and requested_revision == current):
            return ToolResult(
                success=True,
                summary="Reused completed review for the same target and evidence.",
                stdout=json.dumps(run.review, ensure_ascii=False),
            )
        result = await run_delegated_review(
            workspace_root,
            store=store,
            model_complete=model_complete,
            settings=settings,
            profile_id=profile_id,
            run=run,
            subtask_id=subtask_id,
            workspace_revision=requested_revision,
            focus=focus,
            target=resolved,
            emit=emit,
            extra_started={"run_id": run_id},
        )
        return review_tool_result(result)

    return handler


def _submit_review_response(
    store: Any,
    *,
    emit: Callable[[str, str, dict[str, Any] | None], None],
    execution_context: ExecutionContext | None,
):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        run_id = execution_context.run_id if execution_context is not None else None
        if not run_id:
            return ToolResult(
                success=False,
                summary="submit_review_response requires an active run_id.",
                error_code="execution_error",
            )
        return apply_submit_review_response(store, arguments, run_id=run_id, emit=emit)

    return handler


def apply_submit_review_response(
    store: Any,
    arguments: dict[str, Any],
    *,
    run_id: str,
    emit: Callable[[str, str, dict[str, Any] | None], None] | None = None,
) -> ToolResult:
    import json

    from review_agent.harness.reviewer import FindingDisposition, apply_dispositions

    run = store.get_run(run_id)
    if run is None:
        return ToolResult(success=False, summary=f"Unknown run: {run_id}", error_code="execution_error")
    raw_items = arguments.get("dispositions") or []
    items = []
    for item in raw_items:
        payload = item.model_dump() if hasattr(item, "model_dump") else dict(item)
        items.append(FindingDisposition.model_validate(payload))
    known = {item.get("finding_id") for item in (run.review or {}).get("findings") or []}
    missing = [item.finding_id for item in items if item.finding_id not in known]
    if missing:
        return ToolResult(
            success=False,
            summary=f"Unknown finding_id(s): {', '.join(missing)}",
            error_code="invalid_arguments",
        )
    snapshot = apply_dispositions(run, items)
    store.update_run(run_id, review=snapshot)
    for item in items:
        if emit:
            emit(
                "review.disposition",
                f"{item.finding_id} {item.status}",
                {
                    "finding_id": item.finding_id,
                    "status": item.status,
                    "reason": item.reason,
                    "subtask_id": (snapshot or {}).get("subtask_id"),
                },
            )
    return ToolResult(
        success=True,
        summary=f"Recorded {len(items)} review disposition(s).",
        stdout=json.dumps(snapshot.get("dispositions") or [], ensure_ascii=False),
    )


def _default_review_service() -> Any:
    from review_agent.services.review_service import ReviewService

    return ReviewService()


def _tool(
    name: str,
    description: str,
    risk: ToolRisk,
    replay_category: ReplayCategory,
    params_model: type,
    handler,
) -> RegisteredTool:
    return RegisteredTool(
        spec=ToolSpec(
            name=name,
            description=description,
            risk=risk,
            input_schema=params_model.json_schema(),
            replay_category=replay_category,
        ),
        handler=handler,
        params_model=params_model,
    )


def _list_files(workspace: Path):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        root = _resolve_workspace_path(workspace, arguments.get("path", "."))
        limit = int(arguments["limit"])
        files: list[str] = []
        for path in root.rglob("*") if root.is_dir() else [root]:
            if _skip_path(path):
                continue
            files.append(path.relative_to(workspace).as_posix())
            if len(files) >= limit:
                break
        return ToolResult(success=True, summary=f"Listed {len(files)} files.", stdout="\n".join(files))

    return handler


def _read_file(workspace: Path):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        path = _resolve_workspace_path(workspace, arguments["path"])
        if not path.is_file():
            return ToolResult(success=False, summary=f"File not found: {arguments['path']}")
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        start = int(arguments.get("start_line") or 1)
        end = int(arguments.get("end_line") or len(lines))
        content = "\n".join(lines[max(start, 1) - 1 : min(end, len(lines))])
        return ToolResult(
            success=True,
            summary=f"Read {path.relative_to(workspace).as_posix()}.",
            stdout=content,
        )

    return handler


def _search_text(workspace: Path, runner: CommandRunner, ctx: ExecutionContext):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments["query"])
        root = _resolve_workspace_path(workspace, arguments.get("path", "."))
        limit = int(arguments["limit"])
        if shutil.which("rg"):
            result = runner.run(
                [
                    "rg",
                    "-n",
                    "--no-heading",
                    "--",
                    query,
                    root.relative_to(workspace).as_posix(),
                ],
                cwd=workspace,
                timeout=10,
                run_id=ctx.run_id,
                call_id=ctx.call_id,
                workspace_revision=ctx.workspace_revision,
            )
            _store_command_meta(ctx, runner)
            output = "\n".join(result.stdout.splitlines()[:limit])
            refs = _artifact_refs(ctx)
            return ToolResult(
                success=result.returncode in {0, 1},
                summary=f"Search completed for {query!r}.",
                stdout=output,
                artifact_refs=refs,
            )
        matches: list[str] = []
        for path in root.rglob("*"):
            if _skip_path(path) or not path.is_file():
                continue
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if query in line:
                    matches.append(f"{path.relative_to(workspace).as_posix()}:{line_no}:{line.strip()}")
                    if len(matches) >= limit:
                        return ToolResult(
                            success=True,
                            summary=f"Search completed for {query!r}.",
                            stdout="\n".join(matches),
                        )
        return ToolResult(
            success=True,
            summary=f"Search completed for {query!r}.",
            stdout="\n".join(matches),
        )

    return handler


def _git_status(workspace: Path, runner: CommandRunner, ctx: ExecutionContext):
    def handler(_arguments: dict[str, Any]) -> ToolResult:
        result = runner.run(
            ["git", "status", "--short"],
            cwd=workspace,
            timeout=10,
            run_id=ctx.run_id,
            call_id=ctx.call_id,
            workspace_revision=ctx.workspace_revision,
        )
        _store_command_meta(ctx, runner)
        return _command_tool_result(result, ToolRisk.READ_ONLY, "git status completed", ctx)

    return handler


def _git_diff(workspace: Path, runner: CommandRunner, ctx: ExecutionContext):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        command = ["git", "diff"]
        if arguments.get("path"):
            path = _resolve_workspace_path(workspace, arguments["path"])
            command.extend(["--", path.relative_to(workspace).as_posix()])
        result = runner.run(
            command,
            cwd=workspace,
            timeout=10,
            run_id=ctx.run_id,
            call_id=ctx.call_id,
            workspace_revision=ctx.workspace_revision,
        )
        _store_command_meta(ctx, runner)
        return _command_tool_result(result, ToolRisk.READ_ONLY, "git diff completed", ctx)

    return handler


def _run_command(workspace: Path, settings: Settings, runner: CommandRunner, ctx: ExecutionContext):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        argv = [str(part) for part in arguments["argv"]]
        cwd = _resolve_workspace_path(workspace, arguments.get("cwd", "."))
        timeout = int(arguments.get("timeout") or settings.review_agent_command_timeout_seconds)
        result = runner.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            run_id=ctx.run_id,
            call_id=ctx.call_id,
            workspace_revision=ctx.workspace_revision,
        )
        _store_command_meta(ctx, runner)
        meta = ctx.last_execution_meta
        status = meta.get("status")
        summary = "command completed"
        if status == ExecutionStatus.CANCELLED.value:
            summary = "command cancelled"
        elif status == ExecutionStatus.TIMED_OUT.value:
            summary = "command timed out"
        elif not result.success:
            summary = f"command completed with exit code {result.returncode}"
        return ToolResult(
            success=result.success,
            summary=summary,
            stdout=_truncate(result.stdout, settings.review_agent_command_preview_chars),
            stderr=_truncate(result.stderr, settings.review_agent_command_preview_chars),
            risk_level=ToolRisk.VERIFY,
            artifact_refs=_artifact_refs(ctx),
            error_code="cancelled" if status == ExecutionStatus.CANCELLED.value else None,
        )

    return handler


def _load_skill(skill_loader: Any, ctx: ExecutionContext):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        name = str(arguments["name"])
        try:
            loaded = skill_loader.load(name, call_id=ctx.call_id)
        except KeyError:
            return ToolResult(
                success=False,
                summary=f"Unknown skill: {name}",
                error_code="execution_error",
                risk_level=ToolRisk.READ_ONLY,
            )
        payload = loaded.event_payload(call_id=ctx.call_id)
        ctx.pending_skill_events.append(payload)
        ctx.last_execution_meta = {"skill_loaded": payload}
        return ToolResult(
            success=True,
            summary=f"Loaded skill {loaded.record.name} from {loaded.record.source}.",
            stdout=loaded.body,
            risk_level=ToolRisk.READ_ONLY,
        )

    return handler


def _read_skill_resource(skill_loader: Any, ctx: ExecutionContext):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        name = str(arguments["name"])
        rel = str(arguments["path"])
        try:
            text, content_hash = skill_loader.read_resource(name, rel)
        except KeyError:
            return ToolResult(
                success=False,
                summary=f"Unknown skill: {name}",
                error_code="execution_error",
                risk_level=ToolRisk.READ_ONLY,
            )
        except (ValueError, FileNotFoundError, OSError, UnicodeDecodeError) as exc:
            return ToolResult(
                success=False,
                summary=str(exc),
                error_code="execution_error",
                risk_level=ToolRisk.READ_ONLY,
            )
        ctx.last_execution_meta = {
            "skill_resource": {"name": name, "path": rel, "content_hash": content_hash}
        }
        return ToolResult(
            success=True,
            summary=f"Read skill resource {name}:{rel}.",
            stdout=text,
            risk_level=ToolRisk.READ_ONLY,
        )

    return handler


def _run_skill_script(
    workspace: Path,
    settings: Settings,
    runner: CommandRunner,
    skill_loader: Any,
    ctx: ExecutionContext,
):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        name = str(arguments["name"])
        rel = str(arguments["path"])
        try:
            script = skill_loader.resolve_script(name, rel)
        except KeyError:
            return ToolResult(
                success=False,
                summary=f"Unknown skill: {name}",
                error_code="execution_error",
                risk_level=ToolRisk.WRITE_CONFIRM,
            )
        except (ValueError, FileNotFoundError, OSError) as exc:
            return ToolResult(
                success=False,
                summary=str(exc),
                error_code="execution_error",
                risk_level=ToolRisk.WRITE_CONFIRM,
            )
        extra = [str(part) for part in arguments.get("argv") or []]
        argv = ["bash", str(script), *extra]
        cwd = _resolve_workspace_path(workspace, arguments.get("cwd", "."))
        timeout = int(arguments.get("timeout") or settings.review_agent_command_timeout_seconds)
        result = runner.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            run_id=ctx.run_id,
            call_id=ctx.call_id,
            workspace_revision=ctx.workspace_revision,
        )
        _store_command_meta(ctx, runner)
        meta = ctx.last_execution_meta
        status = meta.get("status")
        summary = "skill script completed"
        if status == ExecutionStatus.CANCELLED.value:
            summary = "skill script cancelled"
        elif status == ExecutionStatus.TIMED_OUT.value:
            summary = "skill script timed out"
        elif not result.success:
            summary = f"skill script completed with exit code {result.returncode}"
        return ToolResult(
            success=result.success,
            summary=summary,
            stdout=_truncate(result.stdout, settings.review_agent_command_preview_chars),
            stderr=_truncate(result.stderr, settings.review_agent_command_preview_chars),
            risk_level=ToolRisk.VERIFY,
            artifact_refs=_artifact_refs(ctx),
            error_code="cancelled" if status == ExecutionStatus.CANCELLED.value else None,
        )

    return handler


def _apply_patch(workspace: Path, ctx: ExecutionContext):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        patch = str(arguments["patch"])
        outcome = apply_patch(workspace, patch)
        ctx.last_execution_meta = {
            "patch_hash": outcome.patch_hash,
            "already_applied": outcome.already_applied,
            "file_hashes": [
                {
                    "path": item.path,
                    "before_hash": item.before_hash,
                    "expected_after_hash": item.expected_after_hash,
                    "is_new": item.is_new,
                    "is_delete": item.is_delete,
                }
                for item in outcome.file_hashes
            ],
        }
        return ToolResult(
            success=outcome.success,
            summary=outcome.summary,
            stdout=_truncate(outcome.stdout),
            stderr=_truncate(outcome.stderr),
            changed_files=outcome.changed_files if outcome.success else [],
            verification_hint="Run targeted tests or make check.",
            risk_level=ToolRisk.WRITE_CONFIRM,
            error_code=outcome.error_code,
            artifact_refs=[],
        )

    return handler


def _review_pr(review_service_factory: Callable[[], Any]):
    def handler(arguments: dict[str, Any]) -> ToolResult:
        pr_url = str(arguments["pr_url"])
        report = review_service_factory().review_pr(pr_url)
        return ToolResult(
            success=True,
            summary=f"Review completed with {len(report.findings)} findings.",
            stdout=report.final_report,
            risk_level=ToolRisk.READ_ONLY,
        )

    return handler


def _command_tool_result(result, risk: ToolRisk, summary: str, ctx: ExecutionContext) -> ToolResult:
    return ToolResult(
        success=result.success,
        summary=summary if result.success else f"{summary} with exit code {result.returncode}",
        stdout=_truncate(result.stdout),
        stderr=_truncate(result.stderr),
        risk_level=risk,
        artifact_refs=_artifact_refs(ctx),
    )


def _store_command_meta(ctx: ExecutionContext, runner: CommandRunner) -> None:
    call_id = ctx.call_id
    if not call_id:
        ctx.last_execution_meta = {}
        return
    handle = runner.executor.get(call_id)
    if handle is None:
        ctx.last_execution_meta = {}
        return
    ctx.last_execution_meta = {
        "execution_id": handle.execution_id,
        "argv": list(handle.argv),
        "started_at": handle.started_at,
        "finished_at": handle.finished_at,
        "pid": handle.pid,
        "pgid": handle.pgid,
        "container_name": handle.container_name,
        "container_id": handle.container_id,
        "workspace_revision": handle.workspace_revision or ctx.workspace_revision,
        "stdout_artifact": handle.stdout_path,
        "stderr_artifact": handle.stderr_path,
        "status": handle.status.value,
    }


def _artifact_refs(ctx: ExecutionContext) -> list[str]:
    meta = ctx.last_execution_meta or {}
    refs: list[str] = []
    for key in ("stdout_artifact", "stderr_artifact"):
        value = meta.get(key)
        if value:
            refs.append(str(value))
    return refs


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc", ())) or "(root)"
        parts.append(f"{loc}: {error.get('msg', 'invalid value')}")
    return "Invalid tool arguments: " + "; ".join(parts)


def _resolve_workspace_path(workspace: Path, value: Any) -> Path:
    rel = Path(str(value or "."))
    if rel.is_absolute():
        raise ValueError("path must stay inside the workspace")
    if ".." in rel.parts:
        raise ValueError("path must stay inside the workspace")
    candidate = workspace / rel
    if candidate.is_symlink() or any(parent.is_symlink() for parent in candidate.parents if parent != workspace):
        resolved = candidate.resolve(strict=False)
        if not _is_relative_to(resolved, workspace):
            raise ValueError("symlink must stay inside the workspace")
        return resolved
    resolved = candidate.resolve(strict=False)
    if not _is_relative_to(resolved, workspace):
        raise ValueError("path must stay inside the workspace")
    return resolved


def _skip_path(path: Path) -> bool:
    return any(
        part in {".git", ".venv", "__pycache__", ".memory", ".review-agent", "node_modules"}
        for part in path.parts
    )


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated; see artifact]...\n"


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
