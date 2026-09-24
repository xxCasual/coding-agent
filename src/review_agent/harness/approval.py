from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any

from review_agent.harness.models import ApprovalRequest, ReplayCategory, ToolRisk, ToolSpec


class ApprovalDecision(str, Enum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    BLOCK = "block"


@dataclass(frozen=True)
class ApprovalOutcome:
    decision: ApprovalDecision
    reason: str
    workspace_revision: str | None = None
    patch_hash: str | None = None
    replay_category: str | None = None

    def to_request(self, tool_name: str, arguments: dict[str, Any]) -> ApprovalRequest:
        return ApprovalRequest(
            tool_name=tool_name,
            arguments=arguments,
            reason=self.reason,
            workspace_revision=self.workspace_revision,
            patch_hash=self.patch_hash,
            replay_category=self.replay_category,
        )


class ApprovalPolicy:
    """Risk and replay are separate dimensions; command names are not safety proofs."""

    def __init__(self, mode: str = "confirm") -> None:
        self.mode = mode

    def evaluate(
        self,
        spec: ToolSpec,
        arguments: dict[str, Any],
        *,
        workspace_revision: str | None = None,
        patch_hash: str | None = None,
    ) -> ApprovalOutcome:
        replay = spec.replay_category.value if spec.replay_category else None
        if spec.name == "run_command":
            return self._evaluate_command(
                arguments,
                workspace_revision=workspace_revision,
                replay_category=replay,
            )
        if spec.name == "run_skill_script":
            # Same approval tier as run_command; skill text never expands rights.
            return self._evaluate_command(
                {
                    "argv": ["bash", str(arguments.get("path") or "script"), *list(arguments.get("argv") or [])],
                    "cwd": arguments.get("cwd", "."),
                },
                workspace_revision=workspace_revision,
                replay_category=replay,
            )
        if spec.name == "apply_patch":
            return self._evaluate_patch(
                arguments,
                workspace_revision=workspace_revision,
                patch_hash=patch_hash,
                replay_category=replay,
            )
        if spec.risk == ToolRisk.DANGER_BLOCKED:
            return ApprovalOutcome(
                ApprovalDecision.BLOCK,
                "Tool is blocked by policy.",
                workspace_revision=workspace_revision,
                replay_category=replay,
            )
        if spec.risk == ToolRisk.WRITE_CONFIRM:
            if self.mode == "auto":
                return ApprovalOutcome(
                    ApprovalDecision.ALLOW,
                    "Auto approval mode.",
                    workspace_revision=workspace_revision,
                    patch_hash=patch_hash,
                    replay_category=replay,
                )
            return ApprovalOutcome(
                ApprovalDecision.CONFIRM,
                "Tool may modify workspace state.",
                workspace_revision=workspace_revision,
                patch_hash=patch_hash,
                replay_category=replay,
            )
        return ApprovalOutcome(
            ApprovalDecision.ALLOW,
            "Tool is read-only.",
            workspace_revision=workspace_revision,
            replay_category=replay,
        )

    def _evaluate_patch(
        self,
        arguments: dict[str, Any],
        *,
        workspace_revision: str | None,
        patch_hash: str | None,
        replay_category: str | None,
    ) -> ApprovalOutcome:
        computed_hash = patch_hash
        if computed_hash is None:
            patch = str(arguments.get("patch") or "")
            if patch.strip():
                import hashlib

                computed_hash = hashlib.sha256(patch.encode("utf-8")).hexdigest()
        if self.mode == "auto":
            return ApprovalOutcome(
                ApprovalDecision.ALLOW,
                "Auto approval mode.",
                workspace_revision=workspace_revision,
                patch_hash=computed_hash,
                replay_category=replay_category or ReplayCategory.PATCH_CHECKABLE.value,
            )
        return ApprovalOutcome(
            ApprovalDecision.CONFIRM,
            "Patch write requires confirmation; bound to file versions.",
            workspace_revision=workspace_revision,
            patch_hash=computed_hash,
            replay_category=replay_category or ReplayCategory.PATCH_CHECKABLE.value,
        )

    def _evaluate_command(
        self,
        arguments: dict[str, Any],
        *,
        workspace_revision: str | None,
        replay_category: str | None,
    ) -> ApprovalOutcome:
        command = _coerce_command(arguments.get("argv", arguments.get("command", [])))
        lowered = " ".join(command).lower()
        if _is_dangerous_command(command, lowered):
            return ApprovalOutcome(
                ApprovalDecision.BLOCK,
                "Dangerous command blocked by policy.",
                workspace_revision=workspace_revision,
                replay_category=ReplayCategory.NON_REPLAYABLE.value,
            )
        # Verification-looking commands still have side effects; risk != replay.
        if _is_read_only_command(command):
            return ApprovalOutcome(
                ApprovalDecision.ALLOW,
                "Command classified as read-only (replay may still be non-applicable).",
                workspace_revision=workspace_revision,
                replay_category=ReplayCategory.REPEATABLE_READ.value,
            )
        if _looks_like_verify_command(command):
            if self.mode == "auto":
                return ApprovalOutcome(
                    ApprovalDecision.ALLOW,
                    "Verification command allowed in auto mode; replay is controlled.",
                    workspace_revision=workspace_revision,
                    replay_category=ReplayCategory.CONTROLLED_VERIFY.value,
                )
            return ApprovalOutcome(
                ApprovalDecision.CONFIRM,
                "Verification command may have side effects; confirm execution.",
                workspace_revision=workspace_revision,
                replay_category=ReplayCategory.CONTROLLED_VERIFY.value,
            )
        if self.mode == "auto":
            return ApprovalOutcome(
                ApprovalDecision.ALLOW,
                "Auto approval mode.",
                workspace_revision=workspace_revision,
                replay_category=replay_category or ReplayCategory.NON_REPLAYABLE.value,
            )
        return ApprovalOutcome(
            ApprovalDecision.CONFIRM,
            "Command is not classified as read-only.",
            workspace_revision=workspace_revision,
            replay_category=replay_category or ReplayCategory.NON_REPLAYABLE.value,
        )


def _coerce_command(command: Any) -> list[str]:
    if isinstance(command, str):
        return shlex.split(command)
    if isinstance(command, list):
        return [str(part) for part in command]
    return []


def _is_dangerous_command(command: list[str], lowered: str) -> bool:
    if not command:
        return True
    if command[0] == "sudo":
        return True
    if "git reset --hard" in lowered:
        return True
    if command[0] == "rm" and "-rf" in "".join(command[1:]):
        return True
    return False


def _is_read_only_command(command: list[str]) -> bool:
    if not command:
        return False
    first = command[0]
    if first in {"pwd", "ls", "cat", "sed", "rg", "grep", "find", "wc"}:
        return True
    return first == "git" and len(command) > 1 and command[1] in {"status", "diff", "show", "log"}


def _looks_like_verify_command(command: list[str]) -> bool:
    if not command:
        return False
    text = " ".join(command)
    return any(marker in text for marker in {"pytest", "ruff check", "npm run test", "make check"})
