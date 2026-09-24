from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from review_agent.eval.models import HiddenOutcome


def run_hidden_acceptance(
    script: Path,
    workspace: Path,
    *,
    timeout: int = 60,
    python_executable: str | None = None,
) -> HiddenOutcome:
    """Run trusted acceptance against a workspace. The script is never copied into the snapshot."""
    if not script.is_file():
        return HiddenOutcome(
            passed=False,
            exit_code=None,
            could_not_run=True,
            stderr=f"missing hidden script: {script}",
        )
    workspace = workspace.resolve()
    script = script.resolve()
    try:
        script.relative_to(workspace)
    except ValueError:
        pass
    else:
        return HiddenOutcome(
            passed=False,
            exit_code=None,
            could_not_run=True,
            stderr="hidden acceptance must not live inside the Agent workspace",
        )
    executable = python_executable or sys.executable
    env = {**os.environ, "EVAL_WORKSPACE": str(workspace), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        completed = subprocess.run(
            [executable, str(script)],
            cwd=str(script.parent),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return HiddenOutcome(passed=False, exit_code=None, could_not_run=True, stderr=str(exc))
    except subprocess.TimeoutExpired as exc:
        return HiddenOutcome(
            passed=False,
            exit_code=None,
            could_not_run=True,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr="hidden acceptance timed out",
        )
    return HiddenOutcome(
        passed=completed.returncode == 0,
        exit_code=completed.returncode,
        could_not_run=False,
        stdout=completed.stdout[-4000:],
        stderr=completed.stderr[-4000:],
    )
