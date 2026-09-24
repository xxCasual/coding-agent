from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from review_agent.config import Settings, get_settings
from review_agent.db.codec import merge_usage
from review_agent.graph.context_fetcher import ContextFetcher
from review_agent.harness.models import (
    Message,
    ModelTurnResult,
    ToolCall,
    ToolResult,
    Usage,
    _FrozenModel,
)
from review_agent.harness.review_target import (
    MAX_FILE_BYTES,
    ReviewTarget,
    collect_patch,
    in_scope,
    resolve_target,
    review_cache_key,
)
from review_agent.harness.task_store import AcceptanceSpec, RunRecord, TaskStore
from review_agent.harness.review_state import initial_review_state
from review_agent.harness.workspace_revision import compute_workspace_revision, should_skip_relative
from review_agent.models.context import ContextRequest
from review_agent.models.diff import DiffHunk
from review_agent.models.finding import Finding
from review_agent.reviewers.finding_parser import finding_from_payload, parse_json_object
from review_agent.reviewers.finding_ranker import dedupe_findings, rank_findings
from review_agent.tools.diff_tools import parse_diff_hunks_tool

ReviewStatus = Literal["completed", "failed", "unavailable", "interrupted"]
DispositionStatus = Literal["fixed", "not_adopted"]

REVIEWER_WRITE_TOOLS = frozenset(
    {"apply_patch", "run_command", "run_skill_script", "delegate_review", "submit_review_response"}
)
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
_KEYWORDS = {
    "def",
    "class",
    "return",
    "if",
    "else",
    "for",
    "while",
    "import",
    "from",
    "True",
    "False",
    "None",
}

EmitFn = Callable[[str, str, dict[str, Any] | None], None]
AsyncModelComplete = Callable[..., Awaitable[Any]]


class FindingDisposition(_FrozenModel):
    finding_id: str
    status: DispositionStatus
    reason: str


class ReviewResult(_FrozenModel):
    status: ReviewStatus
    findings: list[Finding] = []
    coverage: list[str] = []
    unchecked: list[str] = []
    warnings: list[str] = []
    usage: Usage | None = None
    workspace_revision: str = ""
    subtask_id: str = ""
    target: ReviewTarget | None = None
    cache_key: str = ""
    read_records: list[dict[str, Any]] = []
    selected_paths: list[str] = []

    def is_passed(self) -> bool:
        return self.status == "completed" and not self.warnings and not self.unchecked

    def as_review_dict(self, *, mode: str = "default", dispositions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {
            "enabled": mode != "off",
            "mode": mode,
            "status": self.status,
            "subtask_id": self.subtask_id,
            "workspace_revision": self.workspace_revision,
            "findings": [item.model_dump(mode="json") for item in self.findings],
            "coverage": list(self.coverage),
            "unchecked": list(self.unchecked),
            "warnings": list(self.warnings),
            "usage": self.usage.model_dump(mode="json") if self.usage else None,
            "dispositions": list(dispositions or []),
            "target": self.target.model_dump(mode="json") if self.target else None,
            "cache_key": self.cache_key,
            "read_records": self.read_records,
            "selected_paths": self.selected_paths,
        }


def forced_review_call_id(run_id: str, workspace_revision: str, cache_key: str = "") -> str:
    identity = json.dumps([run_id, workspace_revision, cache_key], separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def review_is_current(review: dict[str, Any] | None, workspace_revision: str, cache_key: str | None = None) -> bool:
    if not review:
        return False
    target = review.get("target") or {}
    return (review.get("status") == "completed" and review.get("workspace_revision") == workspace_revision
            and target.get("kind", "run_changes") == "run_changes"
            and (cache_key is None or review.get("cache_key") == cache_key))


def reviewer_is_off(run: RunRecord) -> bool:
    review = run.review or {}
    return review.get("mode") == "off" or review.get("enabled") is False


def review_required_for_delivery(run: RunRecord, changed_files: list[str]) -> bool:
    if reviewer_is_off(run):
        return False
    if run.acceptance.mode == "not_applicable":
        return False
    return bool(changed_files)


def dispositions_complete(review: dict[str, Any] | None) -> bool:
    if not review:
        return False
    findings = list(review.get("findings") or [])
    if not findings:
        return True
    by_id = {item.get("finding_id"): item for item in review.get("dispositions") or [] if item.get("finding_id")}
    return all(item.get("finding_id") in by_id and item.get("finding_id") for item in findings)


def collect_contract_evidence(store: TaskStore, run_id: str) -> list[str]:
    records = store.list_tool_executions(run_id)
    lines: list[str] = []
    for record in records:
        name = record.tool_call.name
        if "validate_response_sample" not in name:
            continue
        result = record.result
        if result is None:
            continue
        content = result.stdout or result.summary or ""
        snippet = content[:2000]
        if len(content) > 2000:
            snippet += " [truncated contract evidence; full result retained in tool ledger]"
        digest = hashlib.sha256(result.model_dump_json().encode()).hexdigest()
        lines.append(f"{name}: result_sha256={digest} success={result.success} {snippet}")
    return lines


def _acceptance_text(run: RunRecord) -> str:
    acceptance = run.acceptance if isinstance(run.acceptance, AcceptanceSpec) else AcceptanceSpec()
    return f"mode={acceptance.mode} checks={acceptance.checks} {acceptance.description}"


def review_tool_result(result: ReviewResult, *, call_id: str = "") -> ToolResult:
    return ToolResult(
        success=result.status == "completed",
        summary=f"Review {result.status}: {len(result.findings)} finding(s).",
        stdout=result.model_dump_json(),
        call_id=call_id,
        error_code=None if result.status == "completed" else result.status,
    )


def prepare_review(workspace_root: str | Path, *, run: RunRecord, store: TaskStore,
                   settings: Settings, profile_id: str, target: ReviewTarget | None = None,
                   focus: str = "") -> tuple[ReviewTarget, Path, dict, str]:
    target = target or ReviewTarget()
    if focus:
        if target.focus and target.focus != focus:
            raise ValueError("focus conflicts with target.focus")
        target = target.model_copy(update={"focus": focus})
    resolved, root, material = resolve_target(Path(workspace_root), target)
    key = review_cache_key(resolved, requirement=run.requirement, acceptance=_acceptance_text(run),
        verification=json.dumps(run.verification or {}, sort_keys=True, ensure_ascii=False),
        evidence=collect_contract_evidence(store, run.run_id), profile_id=profile_id,
        settings={"profile": asdict(settings.get_model_profile(profile_id)),
                  "reviewer_max_steps": settings.review_agent_reviewer_max_steps})
    return resolved, root, material, key


async def run_delegated_review(
    workspace_root: str | Path,
    *,
    store: TaskStore,
    model_complete: AsyncModelComplete,
    settings: Settings,
    profile_id: str,
    run: RunRecord,
    subtask_id: str,
    workspace_revision: str,
    focus: str = "",
    emit: EmitFn | None = None,
    extra_started: dict[str, Any] | None = None,
    extra_completed: dict[str, Any] | None = None,
    target: ReviewTarget | None = None,
) -> ReviewResult:
    """Run the local Reviewer once and emit delegate started/completed events."""
    resolved, review_root, material, cache_key = prepare_review(workspace_root, run=run,
        store=store, settings=settings, profile_id=profile_id, target=target, focus=focus)
    if workspace_revision and workspace_revision != compute_workspace_revision(workspace_root):
        raise ValueError("workspace_revision is stale")
    if emit:
        emit(
            "delegate.started",
            f"Reviewer subtask {subtask_id} started",
            {
                "subtask_id": subtask_id,
                "workspace_revision": workspace_revision,
                "target": resolved.model_dump(mode="json"),
                "cache_key": cache_key,
                **(extra_started or {}),
            },
        )
    service = LocalReviewService(
        review_root,
        store=store,
        model_complete=model_complete,
        settings=settings,
        emit=emit,
        profile_id=profile_id,
    )
    result = await service.run(
        run_id=run.run_id,
        subtask_id=subtask_id,
        workspace_revision=resolved.workspace_revision,
        requirement=run.requirement,
        acceptance_text=_acceptance_text(run),
        patch=material["patch"],
        verification_text=json.dumps(run.verification or {}, ensure_ascii=False),
        contract_evidence=collect_contract_evidence(store, run.run_id),
        focus=resolved.focus,
        target=resolved,
        material=material,
        cache_key=cache_key,
    )
    if emit:
        emit(
            "delegate.completed",
            f"Reviewer subtask {subtask_id} {result.status}",
            {
                "subtask_id": subtask_id,
                "status": result.status,
                "findings_count": len(result.findings),
                "workspace_revision": result.workspace_revision,
                "usage": result.usage.model_dump(mode="json") if result.usage else None,
                "target": resolved.model_dump(mode="json"),
                "coverage": result.coverage,
                "unchecked": result.unchecked,
                "read_records": result.read_records,
                **(extra_completed or {}),
            },
        )
    return result


class LocalReviewService:
    """Bounded read-only reviewer loop. Does not append to the parent session log."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        store: TaskStore,
        model_complete: AsyncModelComplete,
        settings: Settings | None = None,
        emit: EmitFn | None = None,
        profile_id: str = "deepseek",
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.store = store
        self.model_complete = model_complete
        self.settings = settings or get_settings()
        self.emit = emit
        self.profile_id = profile_id

    async def run(
        self,
        *,
        run_id: str,
        subtask_id: str,
        workspace_revision: str,
        requirement: str,
        acceptance_text: str,
        patch: str,
        verification_text: str,
        contract_evidence: list[str],
        focus: str = "",
        target: ReviewTarget | None = None,
        material: dict | None = None,
        cache_key: str = "",
    ) -> ReviewResult:
        self.target = target
        self.cache_key = cache_key
        self.selected_paths = (material or {}).get("paths", [])
        self.read_records: list[dict[str, Any]] = []
        current = compute_workspace_revision(self.workspace_root)
        if workspace_revision and workspace_revision != current:
            result = ReviewResult(
                status="failed",
                warnings=["workspace_revision is stale; review is bound to the current file version."],
                workspace_revision=current,
                subtask_id=subtask_id,
                unchecked=["requested revision does not match workspace"],
            )
            return self._persist_review(run_id, result)

        hunks = parse_diff_hunks_tool(patch) if patch.strip() else []
        bundles, unchecked = _collect_context(self.workspace_root, hunks)
        coverage = sorted({hunk.file_path for hunk in hunks})
        unchecked.extend((material or {}).get("unchecked", []))
        if any("[truncated contract evidence" in item for item in contract_evidence):
            unchecked.append("contract evidence: excerpts truncated to 2000 characters per result")
        # Only include complete serialized bundles within the seed budget.
        kept, used = {}, 0
        for key, bundle in bundles.items():
            length = len(bundle.model_dump_json())
            if used + length > 12000:
                unchecked.append(f"{bundle.file_path}: context omitted by seed limit")
                continue
            kept[key] = bundle
            used += length
            self.read_records.extend({"path": item.file_path, "start_line": item.start_line,
                "end_line": item.end_line, "source": "seed_context"} for item in bundle.file_slices)
        bundles = kept
        path_context = ""
        if target and target.kind == "paths":
            parts = []
            for rel in (material or {}).get("paths", []):
                path = self.workspace_root / rel
                if should_skip_relative(rel) or not path.resolve().is_relative_to(self.workspace_root):
                    unchecked.append(f"{rel}: excluded from review")
                    continue
                if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
                    unchecked.append(f"{rel}: missing or oversized file")
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                    if "\0" in content:
                        raise UnicodeError()
                except UnicodeError:
                    unchecked.append(f"{rel}: binary/non-UTF-8 file")
                    continue
                if sum(map(len, parts)) + len(content) > 12000:
                    unchecked.append(f"{rel}: content omitted by seed limit")
                    continue
                parts.append(f"File {rel}:\n{content}\n")
                coverage.append(rel)
                self.read_records.append({"path": rel, "start_line": 1,
                    "end_line": len(content.splitlines()), "source": "seed_file"})
            path_context = "\n".join(parts)
        self.read_records.extend({"path": h.file_path, "source": "patch",
            "start_line": h.new_start or h.old_start, "end_line": h.new_end or h.old_end} for h in hunks)
        seed = _seed_user_content(
            requirement=requirement,
            acceptance_text=acceptance_text,
            patch=patch,
            hunks=hunks,
            bundles=bundles,
            verification_text=verification_text,
            contract_evidence=contract_evidence,
            focus=focus,
            workspace_revision=current,
        )
        seed += "\nTarget: " + (target.model_dump_json() if target else "run_changes")
        seed += "\nSelected paths (not a diff):\n" + path_context
        seed += "\nUnchecked/limitations:\n" + "\n".join(unchecked)
        messages: list[Message] = [
            Message(role="system", content=_REVIEWER_SYSTEM, message_id="reviewer-system", run_id=run_id),
            Message(role="user", content=seed, message_id="reviewer-seed", run_id=run_id),
        ]

        from review_agent.harness.tools import build_reviewer_tool_registry

        registry = build_reviewer_tool_registry(self.workspace_root, settings=self.settings)
        self.review_patch = patch
        remaining = self._remaining_steps(run_id)
        if remaining <= 0:
            result = ReviewResult(
                status="interrupted",
                warnings=["Reviewer budget exhausted before a review result."],
                coverage=coverage,
                unchecked=unchecked,
                workspace_revision=current,
                subtask_id=subtask_id,
            )
            return self._persist_review(run_id, result)

        last_usage: Usage | None = None
        format_retried = False
        for _step in range(remaining):
            try:
                turn = await self.model_complete(
                    [*messages, Message(role="system", run_id=run_id, content=(
                        f"You have {remaining - _step} model response(s) left, including this one. "
                        "Use the supplied source/diff evidence. Return the findings JSON before the budget ends. "
                        "On the last response, report only supported findings; do not request more tools."))],
                    tools=registry.specs(),
                    profile_id=self.profile_id,
                )
            except TimeoutError:
                last_usage = self._account_turn(run_id, ModelTurnResult(), last_usage)
                result = ReviewResult(
                    status="interrupted",
                    warnings=["Reviewer model timed out."],
                    coverage=coverage,
                    unchecked=unchecked,
                    workspace_revision=current,
                    subtask_id=subtask_id,
                    usage=last_usage,
                )
                return self._persist_review(run_id, result)
            last_usage = self._account_turn(run_id, turn, last_usage)
            if getattr(turn, "raw_error", None):
                result = ReviewResult(
                    status="unavailable",
                    warnings=[f"Reviewer model error: {turn.raw_error}"],
                    coverage=coverage,
                    unchecked=unchecked,
                    workspace_revision=current,
                    subtask_id=subtask_id,
                    usage=last_usage,
                )
                return self._persist_review(run_id, result)

            content = getattr(turn, "content", "") or ""
            tool_calls = list(getattr(turn, "tool_calls", None) or [])
            if tool_calls:
                assistant_id = str(uuid.uuid4())
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
                messages.append(
                    Message(
                        role="assistant",
                        content=content,
                        message_id=assistant_id,
                        run_id=run_id,
                        tool_calls=normalized,
                    )
                )
                for tool_call in normalized:
                    result = await self._execute_reviewer_tool(registry, tool_call)
                    if not result.success:
                        unchecked.append(f"{tool_call.name}: {result.summary}")
                    elif tool_call.name in {"read_file", "python_ast_summary"}:
                        rel = str(tool_call.arguments.get("path") or "")
                        coverage.append(rel)
                        self.read_records.append({"path": rel, "source": tool_call.name,
                            "start_line": tool_call.arguments.get("start_line", 1),
                            "end_line": tool_call.arguments.get("end_line"),
                            "output_chars": len(result.stdout)})
                    if "[truncated" in result.stdout or "[truncated" in result.stderr:
                        unchecked.append(f"{tool_call.name}: output truncated")
                    messages.append(
                        Message(
                            role="tool",
                            content=result.observation(),
                            message_id=f"reviewer-tool:{tool_call.call_id}",
                            run_id=run_id,
                            provider_call_id=tool_call.provider_call_id or tool_call.call_id,
                        )
                    )
                continue

            findings, parse_warning = _findings_from_content(content, hunks)
            if parse_warning:
                retry_format = not format_retried and _step + 1 < remaining
                if self.emit:
                    self.emit("review.format_error", parse_warning, {"subtask_id": subtask_id,
                        "retry": retry_format, "response_excerpt": content[:1000]})
                if retry_format:
                    format_retried = True
                    messages.extend([
                        Message(role="assistant", content=content, run_id=run_id),
                        Message(role="user", run_id=run_id, content=(
                            "Your review output could not be parsed: " + parse_warning
                            + ' Return ONLY one valid JSON object with a findings array, e.g. {"findings":[]}. '
                            "Correct the format using the existing evidence; do not invent findings or add prose.")),
                    ])
                    continue
                result = ReviewResult(
                    status="unavailable",
                    warnings=[parse_warning],
                    coverage=coverage,
                    unchecked=unchecked,
                    workspace_revision=current,
                    subtask_id=subtask_id,
                    usage=last_usage,
                )
                return self._persist_review(run_id, result)
            ranked = rank_findings(dedupe_findings(findings))
            result = ReviewResult(
                status="completed",
                findings=ranked,
                coverage=coverage,
                unchecked=unchecked,
                workspace_revision=current,
                subtask_id=subtask_id,
                usage=last_usage,
            )
            return self._persist_review(run_id, result)

        result = ReviewResult(
            status="interrupted",
            warnings=["Reviewer reached its model-step limit without a structured result."],
            coverage=coverage,
            unchecked=unchecked,
            workspace_revision=current,
            subtask_id=subtask_id,
            usage=last_usage,
        )
        return self._persist_review(run_id, result)

    def _remaining_steps(self, run_id: str) -> int:
        run = self.store.get_run(run_id)
        budget = dict(run.budget or {}) if run else {}
        parent_used = int(budget.get("step_count") or 0)
        reviewer_used = int(budget.get("reviewer_step_count") or 0)
        parent_max = int(budget.get("max_steps") or self.settings.review_agent_agent_max_steps)
        reviewer_max = int(budget.get("reviewer_max_steps") or self.settings.review_agent_reviewer_max_steps)
        return max(0, min(parent_max - parent_used, reviewer_max - reviewer_used))

    def _account_turn(self, run_id: str, turn: Any, last_usage: Usage | None) -> Usage | None:
        run = self.store.get_run(run_id)
        budget = dict(run.budget or {}) if run else {}
        budget.setdefault("max_steps", self.settings.review_agent_agent_max_steps)
        budget.setdefault("reviewer_max_steps", self.settings.review_agent_reviewer_max_steps)
        budget["step_count"] = int(budget.get("step_count") or 0) + 1
        budget["reviewer_step_count"] = int(budget.get("reviewer_step_count") or 0) + 1
        usage_obj = getattr(turn, "usage", None)
        incoming = None
        if usage_obj is not None:
            incoming = usage_obj.model_dump(mode="json") if hasattr(usage_obj, "model_dump") else None
        unknown = Usage(profile_id=self.profile_id).model_dump(mode="json")
        incoming = incoming or unknown
        if last_usage is None:
            last_usage = Usage.model_validate(incoming)
        else:
            accumulated = merge_usage(last_usage.model_dump(mode="json"), incoming)
            last_usage = Usage.model_validate(accumulated)
        self.store.update_run(
            run_id,
            usage=merge_usage(run.usage if run else None, incoming),
            budget=budget,
        )
        return last_usage

    async def _execute_reviewer_tool(self, registry: Any, tool_call: ToolCall) -> ToolResult:
        validated = registry.validate_arguments(tool_call.name, tool_call.arguments, call_id=tool_call.call_id)
        if isinstance(validated, ToolResult):
            return validated
        path = tool_call.arguments.get("path")
        if path and should_skip_relative(str(path)):
            return ToolResult(success=False, summary="Path excluded from review snapshot.",
                              call_id=tool_call.call_id, error_code="invalid_arguments")
        if tool_call.name in REVIEWER_WRITE_TOOLS:
            return ToolResult(
                success=False,
                summary=f"Reviewer cannot call write tool {tool_call.name}.",
                call_id=tool_call.call_id,
                error_code="unknown_tool",
            )
        if tool_call.name == "git_status":
            # Snapshots may have no .git; never discover an unrelated ancestor repository.
            return ToolResult(success=True, summary="Selected review paths (snapshot, not a live Git status).",
                              stdout="\n".join(self.selected_paths), call_id=tool_call.call_id)
        if tool_call.name == "git_diff":
            # The original workspace snapshot has no .git; always show this review's diff.
            patch = self.review_patch
            if path:
                scope = ReviewTarget(paths=[str(path)]).paths
                patch = "".join("diff --git " + section for section in patch.split("diff --git ")[1:]
                    if any(section.startswith(f"a/{rel} b/{rel}\n") for rel in self.selected_paths if in_scope(rel, scope)))
            return ToolResult(success=True, summary="Diff for the selected review target.",
                              stdout=patch, call_id=tool_call.call_id)
        tool, payload = validated
        return await registry.execute_async(tool, payload, call_id=tool_call.call_id)

    def _persist_review(self, run_id: str, result: ReviewResult) -> ReviewResult:
        result = result.model_copy(update={"target": self.target, "cache_key": self.cache_key,
            "read_records": self.read_records, "selected_paths": self.selected_paths,
            "coverage": sorted(set(result.coverage)),
            "unchecked": sorted(set(result.unchecked))})
        if result.status == "completed" and compute_workspace_revision(self.workspace_root) != result.workspace_revision:
            result = result.model_copy(update={"status": "failed",
                "warnings": [*result.warnings, "workspace changed during review; result is stale"]})
        run = self.store.get_run(run_id)
        mode = (run.review or {}).get("mode", "default") if run else "default"
        dispositions = list((run.review or {}).get("dispositions") or []) if run else []
        if (result.workspace_revision != ((run.review or {}).get("workspace_revision") if run else None)
                or result.cache_key != ((run.review or {}).get("cache_key") if run else None)):
            dispositions = []
        snapshot = result.as_review_dict(mode=mode, dispositions=dispositions)
        self.store.update_run(run_id, review=snapshot)
        return result


def apply_dispositions(run: RunRecord, items: list[FindingDisposition]) -> dict[str, Any]:
    review = dict(run.review or initial_review_state())
    existing = {item.get("finding_id"): item for item in review.get("dispositions") or [] if item.get("finding_id")}
    for item in items:
        existing[item.finding_id] = item.model_dump(mode="json")
    review["dispositions"] = list(existing.values())
    return review


_REVIEWER_SYSTEM = """You are a local read-only code reviewer for a Python/FastAPI coding agent.
Review only the provided requirement, constraints, current patch, source, and verification evidence.
Report only actionable code defects with a concrete input/condition, incorrect behavior, and supporting evidence.
Do not report style preferences, optional refactors, missing type hints/docstrings, or hypothetical dependency versions as bugs.
Do not put statements that behavior is correct or meets requirements in findings. Return an empty findings list when no defect is supported.
The runtime enforces acceptance separately. Missing execution evidence is a limitation, not a code defect. A passed record is evidence for its stated checks, not proof of all correctness.
Do not invent PR URLs. Do not modify files. You may call read_file, search_text, git_diff, git_status, list_files, or python_ast_summary.
When finished, output ONLY a valid JSON object, with no prose or Markdown. Example shape for a real defect:
{"findings":[{"finding_id":"F1","file_path":"app.py","start_line":1,"end_line":1,"severity":"high","category":"bug","title":"Concrete defect","evidence":"Observed input and code","explanation":"Incorrect result and impact","suggestion":"Specific fix","confidence":0.9,"is_blocking":true}]}.
If there are no issues, return {"findings":[]}. Never claim the review passed if evidence is missing or a tool failed.
"""


def _seed_user_content(
    *,
    requirement: str,
    acceptance_text: str,
    patch: str,
    hunks: list[DiffHunk],
    bundles: dict[str, Any],
    verification_text: str,
    contract_evidence: list[str],
    focus: str,
    workspace_revision: str,
) -> str:
    bundle_json = json.dumps(
        {key: bundle.model_dump(mode="json") for key, bundle in bundles.items()},
        ensure_ascii=False,
        indent=2,
    )
    hunk_json = json.dumps(
        [{"hunk_id": h.hunk_id, "file_path": h.file_path, "change_type": h.change_type, "lines": [h.new_start, h.new_end]} for h in hunks],
        ensure_ascii=False,
    )
    evidence = "\n".join(contract_evidence) or "(none)"
    return (
        f"Requirement:\n{requirement}\n\n"
        f"Acceptance:\n{acceptance_text}\n\n"
        f"workspace_revision: {workspace_revision}\n"
        f"Focus: {focus or '(all changed files)'}\n\n"
        f"Current patch:\n{patch or '(empty git diff)'}\n\n"
        f"Hunks:\n{hunk_json}\n\n"
        f"Context bundles:\n{bundle_json}\n\n"
        f"Verification evidence:\n{verification_text or '(none)'}\n\n"
        f"Existing contract validation evidence (valid|invalid|unsupported + contract_hash):\n{evidence}\n"
    )


def _collect_context(workspace: Path, hunks: list[DiffHunk]) -> tuple[dict[str, Any], list[str]]:
    fetcher = ContextFetcher(workspace)
    bundles: dict[str, Any] = {}
    unchecked: list[str] = []
    for hunk in hunks[:12]:
        request = ContextRequest(
            request_id=f"ctx-{hunk.hunk_id}",
            hunk_id=hunk.hunk_id,
            file_path=hunk.file_path,
            target_symbols=_symbols_from_hunk(hunk.added_code + "\n" + hunk.removed_code),
            required_files=[hunk.file_path],
            need_enclosing_symbol=hunk.language == "python",
            need_imports=hunk.language == "python",
            need_callers=False,
            need_related_tests=hunk.language == "python",
            reason="local review seed",
        )
        bundle, warnings = fetcher.fetch(request, hunk)
        bundles[hunk.hunk_id] = bundle
        unchecked.extend(bundle.missing_context)
        unchecked.extend(item.public_message for item in warnings)
    unchecked.extend(f"{h.file_path}: additional hunk context omitted after 12 hunks" for h in hunks[12:])
    return bundles, unchecked


def _symbols_from_hunk(code: str) -> list[str]:
    symbols = set(_IDENT_RE.findall(code))
    return sorted(symbol for symbol in symbols if symbol not in _KEYWORDS)[:8]


def _findings_from_content(content: str, hunks: list[DiffHunk]) -> tuple[list[Finding], str | None]:
    if not content.strip():
        return [], "Reviewer returned an empty result; this is not a clean review."
    try:
        payload = parse_json_object(content)
    except (json.JSONDecodeError, ValueError, TypeError):
        return [], "Reviewer returned invalid JSON; this is not a clean review."
    if not isinstance(payload, dict):
        return [], "Reviewer payload is not an object; this is not a clean review."
    items = payload.get("findings")
    if items is None:
        return [], "Reviewer JSON omitted findings; unavailable is not 'no issues'."
    if not isinstance(items, list):
        return [], "Reviewer findings is not a list."
    hunk_by_file = {hunk.file_path: hunk for hunk in hunks}
    default_hunk = hunks[0] if hunks else _synthetic_hunk("unknown", 1)
    findings: list[Finding] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not item:
            return [], "Reviewer findings contains an invalid entry; this is not a clean review."
        file_path = str(item.get("file_path") or item.get("file") or default_hunk.file_path)
        hunk = hunk_by_file.get(file_path) or default_hunk
        finding = finding_from_payload(hunk, item, index)
        if finding.hunk_id == hunk.hunk_id and file_path != hunk.file_path:
            finding = finding.model_copy(update={"hunk_id": f"{file_path}:{finding.start_line}:0"})
        findings.append(finding)
    return findings, None


def _synthetic_hunk(file_path: str, line: int) -> DiffHunk:
    return DiffHunk(
        hunk_id=f"{file_path}:{line}:0",
        file_path=file_path,
        change_type="modified",
        raw_diff="",
        new_start=line,
        new_end=line,
    )


def current_patch(workspace: Path) -> str:
    return collect_patch(workspace)["patch"]
