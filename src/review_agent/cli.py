from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

from review_agent.clients.task_api import (
    DEFAULT_API_URL,
    TaskApiClient,
    TaskApiError,
    match_workspace,
    unregistered_hint,
)
from review_agent.config import get_settings
from review_agent.harness.memory import AgentMemory, MemoryStore
from review_agent.services.review_service import ReviewService

LIVE_EVENT_TYPES = {
    "approval.requested",
    "approval.blocked",
    "approval.rejected",
    "tool.started",
    "tool.finished",
    "tool.failed",
    "model.text_delta",
    "verification.completed",
    "verification.repair",
    "run.cancel_requested",
    "run.interrupted",
    "delegate.started",
    "delegate.completed",
    "review.disposition",
}

ACTIVE_STATUSES = {
    "queued",
    "running",
    "waiting_approval",
    "interrupted",
    "needs_attention",
}


class _PlainConsole:
    def print(self, *values: Any, **_: Any) -> None:
        print(*values)


def _console():
    try:
        from rich.console import Console

        return Console()
    except Exception:
        return _PlainConsole()


def main(argv: list[str] | None = None, *, api_client: TaskApiClient | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "health":
        _console().print("ok")
        return 0
    if args.command == "memory":
        return _memory_command(args)
    if args.command == "agent":
        return _agent_command(args, api_client=api_client)
    if args.command == "eval":
        return _eval_command(args)
    if args.command == "review":
        return _review_command(args)
    parser.print_help()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="review-agent")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("health", help="print a health check")

    review = subparsers.add_parser("review", help="run the existing PR review workflow")
    review.add_argument("pr_url")

    agent = subparsers.add_parser("agent", help="open a TaskService coding shell")
    agent.add_argument("cwd", nargs="?", default=None, help="local path; must match a registered workspace")
    agent.add_argument("--workspace", dest="workspace_id", help="registered workspace id")
    agent.add_argument("--session", dest="session_id", help="existing session id")
    agent.add_argument("--api-url", dest="api_url", help="TaskService base URL")
    agent.add_argument("--reviewer", choices=("default", "off"), default="default")
    agent.add_argument("--mode", "--task-mode", dest="task_mode", choices=("develop", "review", "plan"), default="develop")
    agent.add_argument("--review-target", choices=("workspace_changes", "run_changes", "paths"))
    agent.add_argument("--review-path", action="append", default=[])
    agent.add_argument("--review-focus", default="")

    eval_parser = subparsers.add_parser("eval", help="run coding eval tasks via TaskService")
    eval_parser.add_argument("--manifest", required=True, help="path to eval/manifests/dev.json or heldout.json")
    eval_parser.add_argument("--variant", choices=("baseline", "full"), default="baseline")
    eval_parser.add_argument("--out", required=True, help="directory for JSONL results")
    eval_parser.add_argument("--task-id", action="append", dest="task_ids", default=None)
    eval_parser.add_argument("--code-commit", default="", help="git SHA recorded in result rows")
    eval_parser.add_argument("--profile-id", default="deepseek")

    memory = subparsers.add_parser("memory", help="inspect local .memory under --cwd (not TaskService)")
    memory.add_argument("--cwd", default=".")
    memory_subparsers = memory.add_subparsers(dest="memory_command", required=True)
    memory_subparsers.add_parser("list", help="list indexed memories")
    show = memory_subparsers.add_parser("show", help="show one memory by id")
    show.add_argument("memory_id")
    memory_subparsers.add_parser("compact", help="compact memory files if threshold is reached")
    return parser


def _memory_command(args: argparse.Namespace) -> int:
    console = _console()
    memory = AgentMemory(Path(args.cwd))
    memory.store.ensure_initialized()
    if args.memory_command == "list":
        entries = memory.store.list_entries()
        if not entries:
            console.print("No memories recorded.")
            return 0
        for entry in entries:
            console.print(f"{entry.id} [{entry.type.value}] {entry.name} -> {entry.path}")
        return 0
    if args.memory_command == "show":
        entry = memory.store.get_entry(args.memory_id)
        if entry is None:
            console.print(f"Memory not found: {args.memory_id}")
            return 1
        console.print(f"# {entry.name}\n\n{entry.content}")
        return 0
    if args.memory_command == "compact":
        changed = memory.compact_if_needed()
        console.print("Memory compacted." if changed else "No memory compaction needed.")
        return 0
    return 1


def _agent_command(args: argparse.Namespace, *, api_client: TaskApiClient | None = None) -> int:
    console = _console()
    if args.task_mode != "review" and (args.review_target or args.review_path or args.review_focus):
        console.print("Review scope options require --mode review.")
        return 2
    owns_client = api_client is None
    client = api_client or TaskApiClient(args.api_url or get_settings().review_agent_api_url or DEFAULT_API_URL)
    try:
        try:
            workspaces = client.list_workspaces()
        except TaskApiError as exc:
            console.print(f"Cannot reach TaskService API: {exc}")
            return 1
        local_path = Path(args.cwd).expanduser() if args.cwd else None
        matched = match_workspace(workspaces, path=local_path, workspace_id=args.workspace_id)
        if matched is None:
            hint_path = local_path or args.workspace_id
            console.print(unregistered_hint(hint_path if local_path else None))
            if args.workspace_id:
                console.print(f"Unknown workspace id: {args.workspace_id}")
            return 1
        workspace_id = str(matched["workspace_id"])
        if args.session_id:
            session = client.get_session(args.session_id)
            if session.get("workspace_id") != workspace_id:
                console.print(
                    f"Session {args.session_id} belongs to workspace {session.get('workspace_id')}, "
                    f"not {workspace_id}."
                )
                return 1
        else:
            session = client.create_session(workspace_id)
        return _agent_shell(
            client,
            console,
            session=session,
            workspace=matched,
            reviewer=args.reviewer,
            task_mode=args.task_mode,
            review_target=({"kind": args.review_target or ("paths" if args.review_path else "workspace_changes"),
                           "paths": args.review_path, "focus": args.review_focus}
                          if args.review_target or args.review_path or args.review_focus else None),
            memory_cwd=local_path or Path.cwd(),
        )
    finally:
        if owns_client:
            client.close()


def _agent_shell(
    client: TaskApiClient,
    console: Any,
    *,
    session: dict[str, Any],
    workspace: dict[str, Any],
    reviewer: str,
    memory_cwd: Path,
    task_mode: str = "develop",
    review_target: dict[str, Any] | None = None,
) -> int:
    session_id = session["session_id"]
    active_run_id = session.get("active_run_id")
    seen_events: set[tuple[str, int]] = set()
    console.print(f"review-agent coding shell: {workspace.get('display_name') or workspace.get('workspace_id')}")
    console.print(f"session {session_id}  api workspace {workspace.get('workspace_id')}  mode={task_mode}")
    console.print(
        "Use /help, /status, /runs, /artifacts, /cancel, /approve allow|deny, "
        "/resume continue|accept_and_continue|end_task, /review <url>, /exit."
    )
    console.print("Local memory CLI still uses .memory under the given path; it is not the TaskService store.")
    while True:
        try:
            message = input("review-agent> ").strip()
        except EOFError:
            console.print("")
            return 0
        except KeyboardInterrupt:
            console.print("")
            if active_run_id:
                _cancel_and_report(client, console, active_run_id)
            continue
        if not message:
            continue
        if message == "/exit":
            return 0
        if message == "/help":
            console.print(
                "/mode develop|review|plan | /status | /runs | /artifacts [id] | /cancel | "
                "/approve allow|deny | /resume continue|accept_and_continue|end_task | "
                "/review <url> | /memory list|show|compact | /exit"
            )
            continue
        if message == "/mode" or message.startswith("/mode "):
            requested = message.removeprefix("/mode").strip()
            if not requested:
                console.print(f"mode={task_mode}")
                continue
            if requested not in {"develop", "review", "plan"}:
                console.print("Use /mode develop|review|plan.")
                continue
            current = _safe_get_run(client, active_run_id) if active_run_id else None
            if current and current.get("status") in ACTIVE_STATUSES:
                console.print("Finish or cancel the active run before changing mode.")
                continue
            task_mode = requested
            console.print(f"mode={task_mode}; applies to the next run.")
            continue
        if message == "/cancel":
            if not active_run_id:
                console.print("No active run.")
                continue
            active_run_id = _cancel_and_report(client, console, active_run_id)
            continue
        if message.startswith("/approve "):
            decision = message.removeprefix("/approve ").strip().lower()
            _approve(client, console, active_run_id, decision)
            continue
        if message.startswith("/resume"):
            parts = message.split()
            action = parts[1] if len(parts) > 1 else "continue"
            _resume(client, console, active_run_id, action)
            continue
        if message == "/status":
            _print_status(client, console, session_id, active_run_id)
            continue
        if message == "/runs":
            _print_runs(client, console, session_id)
            continue
        if message == "/artifacts" or message.startswith("/artifacts "):
            artifact_id = message.removeprefix("/artifacts").strip()
            _print_artifacts(client, console, active_run_id, artifact_id or None)
            continue
        if message.startswith("/review "):
            pr_url = message.removeprefix("/review ").strip()
            console.print(ReviewService().review_pr(pr_url).final_report)
            continue
        if message.startswith("/memory"):
            _memory_shell(console, memory_cwd, message)
            continue
        try:
            run_id, created = _submit_requirement(
                client,
                session_id,
                message,
                active_run_id=active_run_id,
                reviewer=reviewer,
                task_mode=task_mode,
                review_target=review_target if task_mode == "review" else None,
            )
        except TaskApiError as exc:
            console.print(_format_api_error(exc))
            continue
        active_run_id = run_id
        console.print(f"{'created' if created else 'appended'} run {run_id}")
        try:
            _consume_events(client, console, run_id, seen_events)
        except KeyboardInterrupt:
            active_run_id = _cancel_and_report(client, console, run_id)
            continue
        except TaskApiError as exc:
            console.print(_format_api_error(exc))
        payload = _safe_get_run(client, run_id)
        if payload:
            _print_run_summary(console, payload)
            if payload.get("status") not in ACTIVE_STATUSES:
                active_run_id = None


def _submit_requirement(
    client: TaskApiClient,
    session_id: str,
    requirement: str,
    *,
    active_run_id: str | None,
    reviewer: str,
    task_mode: str = "develop",
    review_target: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    if active_run_id:
        current = client.get_run(active_run_id)
        if current.get("status") in ACTIVE_STATUSES:
            if current.get("task_mode", "develop") != task_mode:
                raise TaskApiError("Active run mode is frozen; finish or cancel it before changing mode.")
            stored = client.append_message(active_run_id, requirement, message_id=str(uuid.uuid4()))
            return stored.get("run_id") or active_run_id, False
    created = client.create_run(
        session_id,
        requirement,
        idempotency_key=str(uuid.uuid4()),
        message_id=str(uuid.uuid4()),
        reviewer=reviewer,
        task_mode=task_mode,
        review_target=review_target,
    )
    return str(created["run_id"]), True


def _consume_events(
    client: TaskApiClient,
    console: Any,
    run_id: str,
    seen: set[tuple[str, int]],
) -> None:
    after = max((seq for key_run, seq in seen if key_run == run_id), default=0)
    for event in client.iter_events(run_id, after_seq=after, seen=seen):
        event_type = str(event.get("type") or "")
        if event_type in LIVE_EVENT_TYPES or event_type.startswith("run."):
            console.print(f"[{event_type}] {event.get('message') or ''}")
            data = event.get("payload") or {}
            if event_type.startswith("delegate."):
                target = data.get("target") or {}
                console.print(f"  target={target.get('kind', 'unknown')} paths={target.get('paths', [])} "
                              f"focus={target.get('focus', '')} revision={target.get('workspace_revision') or data.get('workspace_revision') or 'unknown'}")
                if event_type == "delegate.completed":
                    console.print(f"  findings={data.get('findings_count', 'unknown')} cumulative_usage={_format_usage(data.get('usage'))}")
                    console.print(f"  coverage={data.get('coverage', [])} unchecked={data.get('unchecked', [])}")
            elif event_type == "review.disposition":
                console.print(f"  subtask={data.get('subtask_id')} reason={data.get('reason', '')} (Agent record, not verification)")


def _cancel_and_report(client: TaskApiClient, console: Any, run_id: str) -> str | None:
    try:
        payload = client.cancel_run(run_id)
    except TaskApiError as exc:
        console.print(_format_api_error(exc))
        payload = _safe_get_run(client, run_id) or {}
    status = payload.get("status")
    cancel_requested = bool(payload.get("cancel_requested"))
    if status == "cancelled":
        console.print(f"run {run_id} status=cancelled")
        return None
    if cancel_requested:
        console.print(f"Cancel requested for run {run_id}; status={status} (cancelling, not yet confirmed stopped).")
    else:
        console.print(f"run {run_id} status={status}")
    return run_id if status in ACTIVE_STATUSES else None


def _approve(client: TaskApiClient, console: Any, run_id: str | None, decision: str) -> None:
    if decision not in {"allow", "deny"}:
        console.print("Usage: /approve allow|deny")
        return
    if not run_id:
        console.print("No active run.")
        return
    payload = _safe_get_run(client, run_id)
    pending = (payload or {}).get("pending_approval") or {}
    approval_id = pending.get("approval_id") or (payload or {}).get("pending_approval_id")
    if not approval_id:
        console.print("No pending approval.")
        return
    intent = pending.get("intent") or {}
    console.print(
        f"Approval {approval_id}: {intent.get('name') or 'tool'} "
        f"keys={pending.get('param_summary') or '-'} "
        f"revision={pending.get('workspace_revision') or '-'}"
    )
    try:
        client.decide_approval(str(approval_id), decision)
    except TaskApiError as exc:
        console.print(_format_api_error(exc))
        return
    console.print(f"Recorded decision={decision} for {approval_id}")


def _resume(client: TaskApiClient, console: Any, run_id: str | None, action: str) -> None:
    if action not in {"continue", "accept_and_continue", "end_task"}:
        console.print("Usage: /resume continue|accept_and_continue|end_task")
        return
    if not run_id:
        console.print("No active run.")
        return
    payload = _safe_get_run(client, run_id) or {}
    attention = payload.get("attention") or {}
    try:
        result = client.resume_run(
            run_id,
            action=action,
            call_id=attention.get("call_id"),
            workspace_revision=payload.get("workspace_revision"),
        )
    except TaskApiError as exc:
        console.print(_format_api_error(exc))
        return
    console.print(f"resume action={action} status={result.get('status')}")


def _print_status(client: TaskApiClient, console: Any, session_id: str, run_id: str | None) -> None:
    session = client.get_session(session_id)
    console.print(f"session {session_id} active_run={session.get('active_run_id') or '-'}")
    target = run_id or session.get("active_run_id")
    if not target:
        return
    payload = client.get_run(str(target))
    _print_run_summary(console, payload)


def _print_run_summary(console: Any, payload: dict[str, Any]) -> None:
    status = payload.get("status")
    cancel_requested = bool(payload.get("cancel_requested"))
    label = status
    if cancel_requested and status not in {"cancelled", "failed", "succeeded"}:
        label = f"{status} (cancelling)"
    verification = payload.get("verification") or {}
    verification_status = verification.get("status") or "unverified"
    if verification_status in {"passed", "failed"}:
        if not verification.get("workspace_revision") or not payload.get("workspace_revision"):
            verification_status = "unknown revision (current code unverified)"
        elif verification["workspace_revision"] != payload["workspace_revision"]:
            verification_status = "stale (current code unverified)"
    usage = payload.get("usage")
    usage_text = _format_usage(usage)
    review = payload.get("review") or {}
    review_text = _format_review(review)
    console.print(
        f"run {payload.get('run_id')} mode={payload.get('task_mode', 'develop')} status={label} verification={verification_status} "
        f"revision={payload.get('workspace_revision') or '-'} usage={usage_text} review={review_text}"
    )
    snapshot = payload.get("workspace_snapshot") or {}
    console.print(f"source_run={snapshot.get('source_run_id') or 'registered workspace'} source_revision={snapshot.get('source_revision') or 'unknown'}")
    if payload.get("task_mode") == "review":
        console.print("Review report only; findings do not trigger automatic fixes or imply code acceptance.")
    elif payload.get("task_mode") == "plan":
        console.print("Plan only; proposed checks have not been run.")
    if review.get("enabled") is not False and review.get("mode") != "off":
        target = review.get("target") or payload.get("review_target") or {}
        console.print(f"Reviewer subtask={review.get('subtask_id') or '-'} target={target.get('kind', 'unknown')} "
                      f"paths={target.get('paths', [])} focus={target.get('focus', '')} "
                      f"baseline={target.get('baseline') or 'unknown'} revision={review.get('workspace_revision') or 'unknown'}")
        if target.get("kind") == "workspace_changes":
            console.print("Review covers the original workspace input snapshot, not current task changes.")
        elif review.get("workspace_revision") and review.get("workspace_revision") != payload.get("workspace_revision"):
            console.print("Review is stale or current version is unknown; it does not verify current code.")
        console.print(f"Reviewer cumulative usage (included in run usage): {_format_usage(review.get('usage'))}")
        console.print(f"Selected: {review.get('selected_paths', [])}; provided content: {review.get('coverage', [])} (not full-file coverage)")
        console.print(f"Read records: {review.get('read_records', [])}")
        console.print(f"Unchecked: {review.get('unchecked', [])}; warnings: {review.get('warnings', [])}")
        dispositions = {item.get("finding_id"): item for item in review.get("dispositions") or []}
        for finding in review.get("findings") or []:
            console.print(f"{finding.get('finding_id')} {finding.get('file_path')}:{finding.get('start_line')}-{finding.get('end_line')} "
                          f"{finding.get('severity')} {finding.get('title')}\nEvidence: {finding.get('evidence')}")
            item = dispositions.get(finding.get("finding_id"))
            if item:
                console.print(f"Agent disposition={item.get('status')} reason={item.get('reason')} (not a verification result)")
            elif payload.get("task_mode") != "review":
                console.print("Disposition: pending")
    pending = payload.get("pending_approval") or {}
    if pending:
        intent = pending.get("intent") or {}
        console.print(
            f"pending approval {pending.get('approval_id')}: {intent.get('name')} "
            f"{pending.get('param_summary')} revision={pending.get('workspace_revision') or '-'}"
        )
    if payload.get("needs_attention"):
        attention = payload.get("attention") or {}
        files = attention.get("affected_files") or []
        console.print(f"needs_attention: {attention.get('reason') or ''} files={files}")
        console.print("Reconcile with /resume accept_and_continue or /resume end_task (no unknown-command replay).")


def _print_runs(client: TaskApiClient, console: Any, session_id: str) -> None:
    payload = client.list_session_runs(session_id)
    items = payload.get("items") or []
    if not items:
        console.print("No runs.")
        return
    for item in items:
        console.print(f"{item.get('run_id')} {item.get('status')} {item.get('requirement')}")


def _print_artifacts(client: TaskApiClient, console: Any, run_id: str | None, artifact_id: str | None) -> None:
    if not run_id:
        console.print("No active run.")
        return
    if artifact_id:
        console.print(client.get_artifact_text(run_id, artifact_id))
        return
    items = client.list_artifacts(run_id)
    if not items:
        console.print("No artifacts.")
        return
    for item in items:
        console.print(f"{item.get('artifact_id')} [{item.get('kind')}] {item.get('summary')} ({item.get('size_bytes')} bytes)")


def _memory_shell(console: Any, cwd: Path, message: str) -> None:
    memory = AgentMemory(cwd)
    memory.store.ensure_initialized()
    if message == "/memory list":
        _print_memory_list(console, memory.store)
        return
    if message.startswith("/memory show "):
        memory_id = message.removeprefix("/memory show ").strip()
        entry = memory.store.get_entry(memory_id)
        console.print(f"# {entry.name}\n\n{entry.content}" if entry else f"Memory not found: {memory_id}")
        return
    if message == "/memory compact":
        changed = memory.compact_if_needed()
        console.print("Memory compacted." if changed else "No memory compaction needed.")
        return
    console.print("Usage: /memory list | /memory show <id> | /memory compact")


def _format_usage(usage: dict[str, Any] | None) -> str:
    if not usage:
        return "unknown"
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        tokens = "unknown"
    else:
        tokens = f"in={input_tokens if input_tokens is not None else 'unknown'} out={output_tokens if output_tokens is not None else 'unknown'}"
    cost = usage.get("estimated_cost")
    as_of = usage.get("price_as_of")
    if cost is None:
        price = "unknown"
    else:
        price = f"{cost}" + (f" as_of={as_of}" if as_of else "")
    return f"{tokens} cost={price}"


def _format_review(review: dict[str, Any]) -> str:
    if not review:
        return "off-or-missing"
    mode = review.get("mode")
    if mode == "off" or review.get("enabled") is False:
        return "off"
    status = review.get("status")
    if status == "unavailable":
        return "unavailable (incomplete)"
    if not status:
        return "pending"
    return str(status)


def _format_api_error(exc: TaskApiError) -> str:
    code = f"{exc.code}: " if exc.code else ""
    extra = ""
    if exc.details:
        extra = f" {exc.details}"
    return f"{code}{exc}{extra}"


def _safe_get_run(client: TaskApiClient, run_id: str) -> dict[str, Any] | None:
    try:
        return client.get_run(run_id)
    except TaskApiError:
        return None


def _eval_command(args: argparse.Namespace) -> int:
    from review_agent.eval.runner import run_eval
    from review_agent.eval.summarize import summarize_jsonl

    console = _console()
    proxy_warning = _unreachable_local_proxy_warning()
    if proxy_warning:
        console.print(proxy_warning)
    out_dir = Path(args.out)
    results = run_eval(
        Path(args.manifest),
        variant=args.variant,
        out_dir=out_dir,
        task_ids=args.task_ids,
        code_commit=args.code_commit or None,
        profile_id=args.profile_id,
        settings=get_settings(),
    )
    jsonl = out_dir / f"{args.variant}.jsonl"
    summary = summarize_jsonl(jsonl)
    console.print(
        f"eval {args.variant}: {summary['successes']}/{summary['attempts']} success "
        f"(outcomes={summary['outcomes']})"
    )
    for result in results:
        extra = ""
        if result.outcome != "success" and result.failure_reason:
            extra = f" {result.failure_reason[:240]}"
        console.print(f"  {result.task_id} {result.outcome} hidden={result.hidden_passed}{extra}")
    return 0


def _unreachable_local_proxy_warning() -> str | None:
    import os
    import socket
    from urllib.parse import urlparse

    raw = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY") or os.environ.get("ALL_PROXY")
    if not raw:
        return None
    parsed = urlparse(raw)
    host = parsed.hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return None
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return None
    except OSError:
        return (
            f"warning: {host}:{port} proxy from HTTPS_PROXY/HTTP_PROXY/ALL_PROXY is not reachable; "
            "model calls will fail. Unset those variables or start the proxy before eval."
        )


def _review_command(args: argparse.Namespace) -> int:
    _console().print(ReviewService().review_pr(args.pr_url).final_report)
    return 0


def _print_memory_list(console: Any, store: MemoryStore) -> None:
    entries = store.list_entries()
    if not entries:
        console.print("No memories recorded.")
        return
    for entry in entries:
        console.print(f"{entry.id} [{entry.type.value}] {entry.name} -> {entry.path}")


if __name__ == "__main__":
    raise SystemExit(main())
