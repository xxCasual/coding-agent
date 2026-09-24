from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class ExecutionStatus(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


@dataclass
class ExecutionHandle:
    execution_id: str
    run_id: str | None
    call_id: str | None
    argv: list[str]
    cwd: str
    backend: str
    status: ExecutionStatus
    started_at: str
    workspace_revision: str | None = None
    pid: int | None = None
    pgid: int | None = None
    container_name: str | None = None
    container_id: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    returncode: int | None = None
    preview_stdout: str = ""
    preview_stderr: str = ""
    finished_at: str | None = None


@dataclass(frozen=True)
class ExecutionResult:
    handle: ExecutionHandle
    returncode: int
    stdout: str
    stderr: str
    status: ExecutionStatus

    @property
    def success(self) -> bool:
        return self.status == ExecutionStatus.SUCCEEDED and self.returncode == 0


class Executor:
    """Host process-group or one-shot Docker command execution with cancel handles."""

    def __init__(
        self,
        *,
        backend: str | None = None,
        artifact_root: Path | None = None,
        preview_chars: int = 6000,
        task_image: str = "python:3.12-slim",
        docker_network: str = "none",
    ) -> None:
        if backend is None:
            from review_agent.config import get_settings

            backend = get_settings().review_agent_executor_backend
        self.backend = backend
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else None
        self.preview_chars = preview_chars
        self.task_image = task_image
        self.docker_network = docker_network
        self._lock = threading.RLock()
        self._handles: dict[str, ExecutionHandle] = {}
        self._by_run: dict[str, set[str]] = {}
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._cancel_flags: dict[str, threading.Event] = {}

    def set_artifact_root(self, path: Path | None) -> None:
        self.artifact_root = Path(path).resolve() if path else None

    def start(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        run_id: str | None = None,
        call_id: str | None = None,
        env: dict[str, str] | None = None,
        workspace_revision: str | None = None,
        network: str | None = None,
        timeout: int | None = None,
    ) -> ExecutionHandle:
        if not argv:
            raise ValueError("argv must be non-empty")
        execution_id = call_id or str(uuid.uuid4())
        workdir = str(Path(cwd).resolve() if cwd else Path.cwd())
        artifact_dir = self._artifact_dir(run_id, execution_id)
        stdout_path = artifact_dir / "stdout.log"
        stderr_path = artifact_dir / "stderr.log"
        started_at = datetime.now(timezone.utc).isoformat()
        handle = ExecutionHandle(
            execution_id=execution_id,
            run_id=run_id,
            call_id=call_id,
            argv=list(argv),
            cwd=workdir,
            backend=self.backend,
            status=ExecutionStatus.RUNNING,
            started_at=started_at,
            workspace_revision=workspace_revision,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
        )
        cancel_flag = threading.Event()
        with self._lock:
            self._handles[execution_id] = handle
            self._cancel_flags[execution_id] = cancel_flag
            if run_id:
                self._by_run.setdefault(run_id, set()).add(execution_id)

        if self.backend == "docker":
            self._start_docker(handle, env=env, network=network or self.docker_network, timeout=timeout)
        else:
            self._start_host(handle, env=env)
        return handle

    def wait(self, execution_id: str, *, timeout: float | None = None) -> ExecutionResult:
        with self._lock:
            handle = self._handles.get(execution_id)
            proc = self._procs.get(execution_id)
            cancel_flag = self._cancel_flags.get(execution_id)
        if handle is None:
            raise KeyError(f"unknown execution_id: {execution_id}")
        if handle.status != ExecutionStatus.RUNNING:
            return self._result_from_handle(handle)

        assert proc is not None
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.cancel(execution_id, soft_timeout=2.0)
            handle.status = ExecutionStatus.TIMED_OUT
            handle.returncode = -1
            handle.finished_at = datetime.now(timezone.utc).isoformat()
            handle.preview_stderr = _preview(
                (handle.preview_stderr + f"\nCommand timed out after {timeout}s").strip(),
                self.preview_chars,
            )
            return self._result_from_handle(handle)

        stdout = _read_text(handle.stdout_path)
        stderr = _read_text(handle.stderr_path)
        handle.preview_stdout = _preview(stdout, self.preview_chars)
        handle.preview_stderr = _preview(stderr, self.preview_chars)
        handle.returncode = returncode
        handle.finished_at = datetime.now(timezone.utc).isoformat()
        if cancel_flag is not None and cancel_flag.is_set():
            handle.status = ExecutionStatus.CANCELLED
        elif returncode == 0:
            handle.status = ExecutionStatus.SUCCEEDED
        else:
            handle.status = ExecutionStatus.FAILED
        with self._lock:
            self._procs.pop(execution_id, None)
        self._write_meta(handle)
        return self._result_from_handle(handle)

    def run(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout: int | None = 30,
        run_id: str | None = None,
        call_id: str | None = None,
        env: dict[str, str] | None = None,
        workspace_revision: str | None = None,
        network: str | None = None,
    ) -> ExecutionResult:
        handle = self.start(
            argv,
            cwd=cwd,
            run_id=run_id,
            call_id=call_id,
            env=env,
            workspace_revision=workspace_revision,
            network=network,
            timeout=timeout,
        )
        return self.wait(handle.execution_id, timeout=None if timeout is None else float(timeout))

    def cancel(self, execution_id: str, *, soft_timeout: float = 3.0) -> ExecutionResult:
        with self._lock:
            handle = self._handles.get(execution_id)
            proc = self._procs.get(execution_id)
            cancel_flag = self._cancel_flags.get(execution_id)
        if handle is None:
            raise KeyError(f"unknown execution_id: {execution_id}")
        if handle.status != ExecutionStatus.RUNNING:
            return self._result_from_handle(handle)
        if cancel_flag is not None:
            cancel_flag.set()
        if handle.backend == "docker":
            self._cancel_docker(handle, soft_timeout=soft_timeout)
        else:
            self._cancel_host(handle, proc, soft_timeout=soft_timeout)
        return self.wait(execution_id, timeout=soft_timeout + 5.0)

    def cancel_run(self, run_id: str) -> list[ExecutionResult]:
        with self._lock:
            ids = list(self._by_run.get(run_id, set()))
        return [self.cancel(execution_id) for execution_id in ids]

    def get(self, execution_id: str) -> ExecutionHandle | None:
        with self._lock:
            return self._handles.get(execution_id)

    def active_for_run(self, run_id: str) -> list[ExecutionHandle]:
        with self._lock:
            ids = list(self._by_run.get(run_id, set()))
            return [
                self._handles[eid]
                for eid in ids
                if eid in self._handles and self._handles[eid].status == ExecutionStatus.RUNNING
            ]

    def _artifact_dir(self, run_id: str | None, execution_id: str) -> Path:
        if self.artifact_root is not None:
            path = self.artifact_root / "commands" / execution_id
        else:
            path = Path.cwd() / ".review-agent" / "commands" / execution_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _start_host(self, handle: ExecutionHandle, *, env: dict[str, str] | None) -> None:
        stdout_f = open(handle.stdout_path, "w", encoding="utf-8")  # noqa: SIM115
        stderr_f = open(handle.stderr_path, "w", encoding="utf-8")  # noqa: SIM115
        popen_env = env if env is not None else os.environ.copy()
        proc = subprocess.Popen(
            handle.argv,
            cwd=handle.cwd,
            stdout=stdout_f,
            stderr=stderr_f,
            text=True,
            start_new_session=True,
            env=popen_env,
        )
        handle.pid = proc.pid
        try:
            handle.pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            handle.pgid = proc.pid
        with self._lock:
            self._procs[handle.execution_id] = proc
        # Close parent FDs; child keeps them.
        stdout_f.close()
        stderr_f.close()
        self._write_meta(handle)

    def _start_docker(
        self,
        handle: ExecutionHandle,
        *,
        env: dict[str, str] | None,
        network: str,
        timeout: int | None,
    ) -> None:
        name = f"ra-{handle.run_id or 'local'}-{handle.execution_id}"[:63].replace("_", "-")
        handle.container_name = name
        # Mount only the task cwd (source) at /workspace.
        docker_argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            network,
            "--workdir",
            "/workspace",
            "-v",
            f"{handle.cwd}:/workspace",
        ]
        # Never forward model credentials into the command container.
        blocked = {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"}
        if env:
            for key, value in env.items():
                if key in blocked:
                    continue
                docker_argv.extend(["-e", f"{key}={value}"])
        else:
            docker_argv.extend(["-e", "PYTHONDONTWRITEBYTECODE=1"])
        if timeout is not None:
            docker_argv.extend(["--stop-timeout", str(max(1, min(timeout, 30)))])
        docker_argv.append(self.task_image)
        docker_argv.extend(handle.argv)
        stdout_f = open(handle.stdout_path, "w", encoding="utf-8")  # noqa: SIM115
        stderr_f = open(handle.stderr_path, "w", encoding="utf-8")  # noqa: SIM115
        proc = subprocess.Popen(
            docker_argv,
            stdout=stdout_f,
            stderr=stderr_f,
            text=True,
            start_new_session=True,
        )
        handle.pid = proc.pid
        try:
            handle.pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            handle.pgid = proc.pid
        # Best-effort container id lookup.
        handle.container_id = _docker_inspect_id(name)
        with self._lock:
            self._procs[handle.execution_id] = proc
        stdout_f.close()
        stderr_f.close()
        self._write_meta(handle)

    def _cancel_host(
        self,
        handle: ExecutionHandle,
        proc: subprocess.Popen[str] | None,
        *,
        soft_timeout: float,
    ) -> None:
        pgid = handle.pgid or (proc.pid if proc else None)
        if pgid is None:
            return
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.time() + soft_timeout
        while time.time() < deadline:
            if proc is not None and proc.poll() is not None:
                return
            time.sleep(0.05)
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def _cancel_docker(self, handle: ExecutionHandle, *, soft_timeout: float) -> None:
        name = handle.container_name
        if not name:
            return
        subprocess.run(
            ["docker", "stop", "-t", str(max(1, int(soft_timeout))), name],
            capture_output=True,
            text=True,
            check=False,
        )
        # Ensure exit.
        subprocess.run(["docker", "kill", name], capture_output=True, text=True, check=False)

    def stop_persisted(
        self,
        *,
        pid: int | None = None,
        pgid: int | None = None,
        container_name: str | None = None,
        backend: str | None = None,
        soft_timeout: float = 3.0,
    ) -> str:
        """Stop an orphan process/container from persisted handles.

        Returns already_exited | stopped | unknown.
        """
        if container_name or backend == "docker":
            name = container_name
            if not name:
                return "unknown"
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", name],
                capture_output=True,
                text=True,
                check=False,
            )
            if inspect.returncode != 0:
                return "already_exited"
            running = inspect.stdout.strip().lower() == "true"
            if not running:
                return "already_exited"
            subprocess.run(
                ["docker", "stop", "-t", str(max(1, int(soft_timeout))), name],
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(["docker", "kill", name], capture_output=True, text=True, check=False)
            return "stopped"
        target = pgid or pid
        if target is None:
            return "unknown"
        try:
            os.killpg(target, 0)
        except ProcessLookupError:
            return "already_exited"
        except PermissionError:
            return "unknown"
        except OSError:
            try:
                os.kill(target, 0)
            except ProcessLookupError:
                return "already_exited"
            except OSError:
                return "unknown"
        try:
            os.killpg(target, signal.SIGTERM)
        except ProcessLookupError:
            return "already_exited"
        except OSError:
            try:
                os.kill(target, signal.SIGTERM)
            except ProcessLookupError:
                return "already_exited"
            except OSError:
                return "unknown"
        deadline = time.time() + soft_timeout
        while time.time() < deadline:
            try:
                os.killpg(target, 0)
            except ProcessLookupError:
                return "stopped"
            except OSError:
                try:
                    os.kill(target, 0)
                except ProcessLookupError:
                    return "stopped"
            time.sleep(0.05)
        try:
            os.killpg(target, signal.SIGKILL)
        except ProcessLookupError:
            return "stopped"
        except OSError:
            try:
                os.kill(target, signal.SIGKILL)
            except OSError:
                return "unknown"
        return "stopped"

    def _result_from_handle(self, handle: ExecutionHandle) -> ExecutionResult:
        stdout = handle.preview_stdout or _preview(_read_text(handle.stdout_path), self.preview_chars)
        stderr = handle.preview_stderr or _preview(_read_text(handle.stderr_path), self.preview_chars)
        return ExecutionResult(
            handle=handle,
            returncode=handle.returncode if handle.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            status=handle.status,
        )

    def _write_meta(self, handle: ExecutionHandle) -> None:
        if not handle.stdout_path:
            return
        meta_path = Path(handle.stdout_path).with_name("execution.json")
        payload: dict[str, Any] = {
            "execution_id": handle.execution_id,
            "run_id": handle.run_id,
            "call_id": handle.call_id,
            "argv": handle.argv,
            "cwd": handle.cwd,
            "backend": handle.backend,
            "status": handle.status.value,
            "started_at": handle.started_at,
            "finished_at": handle.finished_at,
            "pid": handle.pid,
            "pgid": handle.pgid,
            "container_name": handle.container_name,
            "container_id": handle.container_id,
            "workspace_revision": handle.workspace_revision,
            "returncode": handle.returncode,
            "stdout_path": handle.stdout_path,
            "stderr_path": handle.stderr_path,
        }
        meta_path.write_text(
            __import__("json").dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def _preview(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated; see artifact]...\n"


def _read_text(path: str | None) -> str:
    if not path:
        return ""
    file_path = Path(path)
    if not file_path.is_file():
        return ""
    return file_path.read_text(encoding="utf-8", errors="replace")


def _docker_inspect_id(name: str) -> str | None:
    completed = subprocess.run(
        ["docker", "inspect", "-f", "{{.Id}}", name],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None
