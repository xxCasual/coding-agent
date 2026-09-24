#!/usr/bin/env python3
"""Run a demo-fastapi coding task against the compose API (auto-approves writes)."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_API = "http://127.0.0.1:8000"
REQUIREMENT = (
    "Fix POST /items so quantity is stored and returned as a JSON integer, not a string. "
    "Run pytest in the workspace to verify."
)
TERMINAL = {"succeeded", "completed", "failed", "cancelled", "interrupted"}
_PATCH_RECONCILE_MARKERS = (
    "partially applied",
    "not checkable",
    "files must be reconciled",
    "missing patch text",
)


def api_base(argv: list[str] | None = None) -> str:
    args = sys.argv if argv is None else argv
    if len(args) > 1 and args[1].startswith(("http://", "https://")):
        return args[1]
    return DEFAULT_API


def request(
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    *,
    api: str | None = None,
) -> dict:
    data = None
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(f"{api or DEFAULT_API}{path}", data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def _attention(run: dict) -> dict:
    payload = run.get("attention") or {}
    return payload if isinstance(payload, dict) else {}


def _attention_call_id(run: dict) -> str:
    return str(_attention(run).get("call_id") or "")


def is_patch_reconciliation(run: dict) -> bool:
    """True when needs_attention is a patch-reconcile block that must not auto-loop."""
    if str(run.get("status") or "") != "needs_attention":
        return False
    reason = str(_attention(run).get("reason") or "").lower()
    return any(marker in reason for marker in _PATCH_RECONCILE_MARKERS)


def should_auto_resume(run: dict, *, resumed_call_ids: set[str]) -> bool:
    """Auto-resume needs_attention at most once per call_id; never on patch reconcile."""
    if str(run.get("status") or "") != "needs_attention":
        return False
    if is_patch_reconciliation(run):
        return False
    call_id = _attention_call_id(run) or "_unknown"
    return call_id not in resumed_call_ids


def dump_run(run: dict) -> None:
    print(json.dumps(run, indent=2, ensure_ascii=False))


def main() -> int:
    api = api_base()

    def call(method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        return request(method, path, body, headers, api=api)

    session = call("POST", "/api/sessions", {"workspace_id": "demo-fastapi"})
    session_id = session["session_id"]
    created = call(
        "POST",
        f"/api/sessions/{session_id}/runs",
        {"requirement": REQUIREMENT, "reviewer": "off"},
        headers={"Idempotency-Key": f"compose-demo-{int(time.time())}"},
    )
    run_id = created["run_id"]
    print(f"run_id={run_id} status={created['status']}")

    deadline = time.time() + 900
    resumed_call_ids: set[str] = set()
    while time.time() < deadline:
        run = call("GET", f"/api/runs/{run_id}")
        status = run["status"]
        attention = _attention(run)
        print(
            f"poll status={status} phase={run.get('phase')} "
            f"dispatch_pending={run.get('dispatch_pending')} "
            f"attention_call_id={attention.get('call_id')} "
            f"attention_reason={attention.get('reason')}"
        )
        if status == "waiting_approval":
            approval_id = run.get("pending_approval_id") or (run.get("pending_approval") or {}).get("approval_id")
            if approval_id:
                call("POST", f"/api/approvals/{approval_id}/decision", {"decision": "allow"})
                print(f"approved {approval_id}")
        if status == "needs_attention":
            if is_patch_reconciliation(run) or not should_auto_resume(run, resumed_call_ids=resumed_call_ids):
                print("refusing to auto-resume needs_attention (reconciliation or repeated call_id)", file=sys.stderr)
                dump_run(run)
                return 1
            call(
                "POST",
                f"/api/runs/{run_id}/resume",
                {
                    "action": "accept_and_continue",
                    "workspace_revision": run.get("workspace_revision"),
                },
            )
            resumed_call_ids.add(_attention_call_id(run) or "_unknown")
            print("resumed from needs_attention")
        if status in TERMINAL:
            dump_run(run)
            return 0 if status in {"succeeded", "completed"} else 1
        time.sleep(5)

    print("timeout waiting for terminal status", file=sys.stderr)
    try:
        dump_run(call("GET", f"/api/runs/{run_id}"))
    except Exception:
        pass
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print(exc.read().decode("utf-8"), file=sys.stderr)
        raise SystemExit(3) from exc
