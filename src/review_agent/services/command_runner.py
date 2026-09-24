from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from review_agent.observability import get_logger, log_event
from review_agent.services.executor import ExecutionStatus, Executor

logger = get_logger(__name__)


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


class CommandRunner:
    """Sync façade over Executor; preserves the historical run() call surface."""

    def __init__(self, executor: Executor | None = None) -> None:
        self.executor = executor or Executor(backend="host")

    def run(
        self,
        command: list[str],
        cwd: str | Path | None = None,
        timeout: int = 30,
        *,
        run_id: str | None = None,
        call_id: str | None = None,
        env: dict[str, str] | None = None,
        workspace_revision: str | None = None,
    ) -> CommandResult:
        started = time.perf_counter()
        command_text = " ".join(command)
        log_event(
            logger,
            logging.INFO,
            "command.start",
            "Command started",
            stage="command",
            command=command_text,
        )
        try:
            result = self.executor.run(
                command,
                cwd=cwd,
                timeout=timeout,
                run_id=run_id,
                call_id=call_id,
                env=env,
                workspace_revision=workspace_revision,
            )
        except OSError as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                logger,
                logging.ERROR,
                "command.failure",
                "Command execution failed",
                stage="command",
                duration_ms=duration_ms,
                error_code="tool.execution_failed",
                command=command_text,
                exception_type=type(exc).__name__,
            )
            return CommandResult(
                command=command,
                returncode=-1,
                stdout="",
                stderr=f"{type(exc).__name__}: {exc}",
            )

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        timed_out = result.status == ExecutionStatus.TIMED_OUT
        cancelled = result.status == ExecutionStatus.CANCELLED
        ok = result.success
        log_event(
            logger,
            logging.INFO if ok else logging.ERROR,
            "command.success" if ok else "command.failure",
            "Command finished",
            stage="command",
            duration_ms=duration_ms,
            command=command_text,
            returncode=result.returncode,
            status=result.status.value,
        )
        stderr = result.stderr
        if timed_out and "timed out" not in stderr.lower():
            stderr = (stderr + f"\nCommand timed out after {timeout}s").strip()
        if cancelled and "cancelled" not in stderr.lower():
            stderr = (stderr + "\nCommand cancelled").strip()
        return CommandResult(command, result.returncode, result.stdout, stderr)
