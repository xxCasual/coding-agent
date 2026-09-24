---
name: fix-failing-tests
description: Reproduce failing tests, read related implementation, apply a minimal fix, then re-run the target checks and needed regression. Use when tests fail, CI is red, or a user asks to fix failing pytest/unittest cases.
---

# Fix failing tests

## When to use

- Existing tests fail locally or in CI.
- A recent change broke assertions, imports, or fixtures.
- The acceptance gate is command-based test runs.

## Steps

1. **Reproduce** — Run the exact failing command from the task acceptance or the reported failure. Capture exit code and relevant stdout/stderr before changing code.
2. **Locate** — Read the failing test and the implementation it exercises. Prefer the smallest related surface; avoid unrelated refactors.
3. **Minimal fix** — Change only what makes the failure go away while preserving intended behavior. Do not weaken assertions unless the requirement explicitly changed.
4. **Verify** — Re-run the same target check. Add a narrow regression run if the fix touches shared helpers.
5. **Record unresolved items** — If some failures remain out of scope, list them explicitly.

## Tools

Use workspace tools (`run_command`, `read_file`, `apply_patch`, search). Optional skill script: `scripts/capture_pytest.sh` via `run_skill_script` (same Executor/approval as `run_command`). Skill text does **not** expand permissions.

## Required evidence

Leave all of the following in the task trail (commands, tool results, or delivery notes):

| Evidence | Notes |
| --- | --- |
| Before exit code / output | From the first reproduce run |
| After exit code / output | From the verification run |
| Patch | Unified diff or `apply_patch` record |
| Unresolved items | Empty list if fully fixed |

See [references/evidence-checklist.md](references/evidence-checklist.md).
