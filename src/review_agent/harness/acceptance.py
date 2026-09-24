from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from review_agent.harness.models import VerificationRecord, VerificationStatus
from review_agent.harness.task_store import AcceptanceSpec
from review_agent.harness.workspace_revision import compute_workspace_revision
from review_agent.services.command_runner import CommandRunner
from review_agent.services.executor import Executor


@dataclass(frozen=True)
class AcceptanceOutcome:
    record: VerificationRecord
    passed: bool
    details: list[str]


class AcceptanceGate:
    """Runs frozen goal checks and binds results to workspace_revision."""

    def __init__(
        self,
        workspace_root: str | Path,
        *,
        command_timeout: int = 30,
        executor: Executor | None = None,
        run_id: str | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.command_timeout = command_timeout
        self.executor = executor
        self.run_id = run_id
        self._runner = CommandRunner(executor) if executor is not None else CommandRunner()

    def evaluate(self, spec: AcceptanceSpec) -> AcceptanceOutcome:
        revision = compute_workspace_revision(self.workspace_root)
        if spec.mode == "not_applicable":
            record = VerificationRecord(
                status=VerificationStatus.NOT_APPLICABLE,
                command=None,
                exit_code=None,
                workspace_revision=revision,
                evidence_refs=[],
            )
            return AcceptanceOutcome(record=record, passed=True, details=["explanation-only task"])
        if spec.mode != "commands" or not spec.checks:
            record = VerificationRecord(
                status=VerificationStatus.UNVERIFIED,
                command=None,
                exit_code=None,
                workspace_revision=revision,
                evidence_refs=[],
            )
            return AcceptanceOutcome(
                record=record,
                passed=False,
                details=["no executable acceptance configured"],
            )

        details: list[str] = []
        evidence: list[str] = []
        last_command: str | None = None
        last_exit: int | None = None
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        for index, argv in enumerate(spec.checks):
            last_command = " ".join(argv)
            call_id = f"accept-{self.run_id or 'local'}-{index}"
            result = self._runner.run(
                argv,
                cwd=self.workspace_root,
                timeout=self.command_timeout,
                run_id=self.run_id,
                call_id=call_id,
                env=env,
                workspace_revision=revision,
            )
            last_exit = result.returncode
            handle = self._runner.executor.get(call_id)
            if handle and handle.stdout_path:
                evidence.append(handle.stdout_path)
            if handle and handle.stderr_path:
                evidence.append(handle.stderr_path)
            evidence.append(f"check:{last_command}:exit={last_exit}:rev={revision}")
            snippet = (result.stdout or "")[-500:] + (result.stderr or "")[-500:]
            if result.returncode != 0:
                details.append(f"failed: {last_command}\n{snippet}".strip())
                # Recompute revision after failed check (commands may have written files).
                revision = compute_workspace_revision(self.workspace_root)
                record = VerificationRecord(
                    status=VerificationStatus.FAILED,
                    command=last_command,
                    exit_code=last_exit,
                    workspace_revision=revision,
                    evidence_refs=evidence,
                )
                return AcceptanceOutcome(record=record, passed=False, details=details)
            details.append(f"passed: {last_command}")

        revision = compute_workspace_revision(self.workspace_root)
        record = VerificationRecord(
            status=VerificationStatus.PASSED,
            command=last_command,
            exit_code=last_exit,
            workspace_revision=revision,
            evidence_refs=evidence,
        )
        return AcceptanceOutcome(record=record, passed=True, details=details)
