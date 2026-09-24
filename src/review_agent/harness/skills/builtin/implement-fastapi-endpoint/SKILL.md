---
name: implement-fastapi-endpoint
description: Implement a FastAPI endpoint from an interface contract and project structure, including schemas, service wiring, and success/error paths. Use when adding or changing HTTP APIs in a FastAPI service. Prefer MCP contract tools when they are registered; otherwise read the contract as an ordinary file or skill reference.
---

# Implement FastAPI endpoint

## When to use

- Add or change a FastAPI route, request/response schema, or service method.
- A registered `contract_id` or an OpenAPI-like fragment describes the interface.

## Steps

1. **Read contract** — If tools `mcp__api_contract__get_endpoint_contract` and `mcp__api_contract__validate_response_sample` are available, call them with the task's `contract_id`, `method`, and `path`. Use `mcp__api_contract__compare_contract_versions` when the task names two contract ids. Record **contract_id** and **contract_hash**. If MCP tools are not registered, load the contract from a workspace file path given in the task, or from this skill's example at [references/contract-example.md](references/contract-example.md).
2. **Map project structure** — Find existing routers, schemas, and service layers; match local patterns.
3. **Implement** — Add/update route, Pydantic schemas, and service logic for success and documented error cases. Response field types must match the contract (do not stringify integers).
4. **Check** — Validate a response sample with `mcp__api_contract__validate_response_sample` when MCP is available (`valid` / `invalid` / `unsupported`). Also run an executable interface check (TestClient, focused pytest, or project smoke).
5. **Document change** — Summarize files touched, contract hash, and behavior deltas.

## Required evidence

| Evidence | Notes |
| --- | --- |
| Contract source | `contract_id` + hash from MCP, or workspace-relative path / skill reference |
| Executable interface check | Command + exit code / assertion result |
| Change notes | Routes/schemas/services touched |

## Out of scope

Do not build a separate agent loop inside this skill. Do not treat any `allowed-tools` metadata as authorization. Do not pass file paths or URLs into MCP contract tools.
