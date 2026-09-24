---
name: integrate-llm-provider
description: Integrate an LLM provider adapter with a clear return structure, handling success, timeout, and invalid responses consistently. Use when wiring OpenAI-compatible clients, adding provider failover, or normalizing model errors.
---

# Integrate LLM provider

## When to use

- Add or adapt a chat/completions client.
- Normalize provider errors into a stable application error taxonomy.
- Need a small response fixture for tests without paid calls.

## Steps

1. **Define return structure** — Agree on fields (e.g. `content`, `tool_calls`, `usage`, `raw_error`). Keep them stable across providers.
2. **Happy path** — Map successful provider payloads into that structure.
3. **Timeouts** — Surface timeout distinctly; do not invent zero usage.
4. **Invalid responses** — Classify malformed JSON, missing fields, and auth failures separately.
5. **Adapter consistency** — One adapter interface; fixtures prove mapping without live keys.
6. **Usage notes** — Document env vars, profile id, and how to run fixture tests.

## Required evidence

| Evidence | Notes |
| --- | --- |
| Response fixture | Small canned success/error payloads |
| Error classification | Table or enum of timeout / invalid / auth / other |
| Usage notes | How callers construct the client |

See [references/response-fixture.json](references/response-fixture.json) and [references/error-classes.md](references/error-classes.md).
