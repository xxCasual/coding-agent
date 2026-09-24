from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
REGISTRY_HINT = """Workspace is not registered. On the API host, add it to $REVIEW_AGENT_DATA_ROOT/workspace_registry.json:

{{
  "workspaces": [
    {{"id": "example", "display_name": "Example", "path": "{path}"}}
  ]
}}

Do not pass an unregistered absolute path to the API. Restart the API after editing the registry.
"""


class TaskApiError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, status_code: int = 0, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


class TaskApiClient:
    """HTTP client for the coding TaskService API. CLI and tests inject the same surface."""

    def __init__(
        self,
        base_url: str = DEFAULT_API_URL,
        *,
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_http = http is None
        self.http = http or httpx.Client(base_url=self.base_url, timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> TaskApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def list_workspaces(self) -> list[dict[str, Any]]:
        return self._get_json("/api/workspaces")

    def create_session(self, workspace_id: str) -> dict[str, Any]:
        return self._request_json("POST", "/api/sessions", json={"workspace_id": workspace_id})

    def list_sessions(self, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get_json("/api/sessions", params=params)

    def get_session(self, session_id: str, *, limit: int = 50, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get_json(f"/api/sessions/{quote(session_id, safe='')}", params=params)

    def list_session_runs(self, session_id: str, *, limit: int = 20, cursor: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._get_json(f"/api/sessions/{quote(session_id, safe='')}/runs", params=params)

    def create_run(
        self,
        session_id: str,
        requirement: str,
        *,
        idempotency_key: str,
        message_id: str | None = None,
        reviewer: str = "default",
        task_mode: str = "develop",
        review_target: dict[str, Any] | None = None,
        profile_id: str = "deepseek",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"requirement": requirement, "reviewer": reviewer, "profile_id": profile_id}
        body["task_mode"] = task_mode
        if review_target is not None:
            body["review_target"] = review_target
        if message_id:
            body["message_id"] = message_id
        return self._request_json(
            "POST",
            f"/api/sessions/{quote(session_id, safe='')}/runs",
            json=body,
            headers={"Idempotency-Key": idempotency_key},
        )

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._get_json(f"/api/runs/{quote(run_id, safe='')}")

    def append_message(self, run_id: str, content: str, *, message_id: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"content": content}
        if message_id:
            body["message_id"] = message_id
        return self._request_json("POST", f"/api/runs/{quote(run_id, safe='')}/messages", json=body)

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        return self._request_json("POST", f"/api/runs/{quote(run_id, safe='')}/cancel")

    def resume_run(
        self,
        run_id: str,
        *,
        action: str = "continue",
        call_id: str | None = None,
        workspace_revision: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"action": action}
        if call_id:
            body["call_id"] = call_id
        if workspace_revision:
            body["workspace_revision"] = workspace_revision
        return self._request_json("POST", f"/api/runs/{quote(run_id, safe='')}/resume", json=body)

    def decide_approval(self, approval_id: str, decision: str) -> dict[str, Any]:
        return self._request_json(
            "POST",
            f"/api/approvals/{quote(approval_id, safe='')}/decision",
            json={"decision": decision},
        )

    def list_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return self._get_json(f"/api/runs/{quote(run_id, safe='')}/artifacts")

    def get_artifact_text(self, run_id: str, artifact_id: str) -> str:
        response = self.http.get(self._url(f"/api/runs/{quote(run_id, safe='')}/artifacts/{quote(artifact_id, safe='')}"))
        self._raise_for_status(response)
        return response.text

    def iter_events(self, run_id: str, *, after_seq: int = 0, seen: set[tuple[str, int]] | None = None) -> Iterator[dict[str, Any]]:
        dedupe = seen if seen is not None else set()
        headers = {}
        if after_seq:
            headers["Last-Event-ID"] = str(after_seq)
        with self.http.stream(
            "GET",
            self._url(f"/api/runs/{quote(run_id, safe='')}/events"),
            params={"after": after_seq},
            headers=headers,
        ) as response:
            self._raise_for_status(response)
            for event in _parse_sse(response.iter_lines()):
                seq = event.get("seq")
                if isinstance(seq, int):
                    key = (str(event.get("run_id") or run_id), seq)
                    if key in dedupe:
                        continue
                    dedupe.add(key)
                yield event

    def _url(self, path: str) -> str:
        if str(self.http.base_url):
            return path
        return f"{self.base_url}{path}"

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self.http.get(self._url(path), params=params)
        self._raise_for_status(response)
        return response.json()

    def _request_json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self.http.request(method, self._url(path), **kwargs)
        self._raise_for_status(response)
        if not response.content:
            return {}
        return response.json()

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        code = None
        message = f"HTTP {response.status_code}"
        details: dict[str, Any] = {}
        try:
            payload = response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = payload.get("detail", payload)
            if isinstance(detail, dict):
                code = detail.get("code")
                message = str(detail.get("message") or message)
                extra = detail.get("details")
                if isinstance(extra, dict):
                    details = extra
            elif detail is not None:
                message = str(detail)
        raise TaskApiError(message, code=code, status_code=response.status_code, details=details)


def match_workspace(
    workspaces: list[dict[str, Any]],
    *,
    path: str | Path | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any] | None:
    if workspace_id:
        wanted = workspace_id.strip()
        for item in workspaces:
            if item.get("workspace_id") == wanted:
                return item
        return None
    if path is None:
        return None
    resolved = str(Path(path).expanduser().resolve())
    for item in workspaces:
        raw = item.get("path")
        if not raw:
            continue
        if str(Path(str(raw)).expanduser().resolve()) == resolved:
            return item
    return None


def unregistered_hint(path: str | Path | None = None) -> str:
    shown = str(Path(path).expanduser().resolve()) if path else "/absolute/path/to/repo"
    return REGISTRY_HINT.format(path=shown)


def _parse_sse(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    event_name = ""
    event_id = ""
    data_lines: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if line.endswith("\r"):
            line = line[:-1]
        if line == "":
            if data_lines:
                yield _sse_event(event_name, event_id, "\n".join(data_lines))
            event_name = ""
            event_id = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "id":
            event_id = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield _sse_event(event_name, event_id, "\n".join(data_lines))


def _sse_event(event_name: str, event_id: str, data: str) -> dict[str, Any]:
    payload: dict[str, Any]
    try:
        parsed = json.loads(data)
        payload = parsed if isinstance(parsed, dict) else {"data": parsed}
    except json.JSONDecodeError:
        payload = {"data": data}
    if event_id and payload.get("seq") is None:
        try:
            payload["seq"] = int(event_id)
        except ValueError:
            payload["sse_id"] = event_id
    if event_name and not payload.get("type"):
        payload["type"] = event_name
    return payload
