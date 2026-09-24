# Code Review: portfolio/demo-python-service#42

- PR: [Harden user input parsing](https://github.com/portfolio/demo-python-service/pull/42)
- Author: codex-demo
- Base: `main` `00000000`
- Head: `feature/review-agent-demo` `11111111`
- Changed files: 2

## Risk Overview

- Total findings: 1
- Blocking findings: 1
- Severity mix: high=1

## Blocking Findings

### 1. Unsafe dynamic code execution

- ID: `F-demo-1`
- Location: `src/app.py:10`
- Severity: `high`
- Category: `security`
- Confidence: 0.90
- Evidence: The added code calls eval(user_input).
- Explanation: Dynamic execution can run untrusted input with service privileges.
- Suggestion: Replace eval with an explicit parser or dispatch table.

## Non-Blocking Findings

No non-blocking findings.

## Tool Evidence

- `python_syntax_check` `src/app.py`: passed - Python syntax is valid
- `pytest_targeted`: passed - targeted pytest passed
