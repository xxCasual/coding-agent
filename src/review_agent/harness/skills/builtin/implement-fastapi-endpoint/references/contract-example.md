# Example API contract (`items-v1`, OpenAPI 3.1)

Prefer MCP when registered:

- `mcp__api_contract__get_endpoint_contract` with `contract_id=items-v1`, `method=POST`, `path=/items`
- `mcp__api_contract__validate_response_sample` against the same endpoint
- `mcp__api_contract__compare_contract_versions` with `items-v1` / `items-v2` when comparing versions

The same contract is also stored as `examples/contracts/items-v1.json`. If MCP is not configured, treat this file (or a workspace copy) as a normal readable contract.

## `POST /items`

### Request

```json
{
  "name": "string (1-80)",
  "quantity": "integer >= 1",
  "priority": "optional enum: low | high"
}
```

### Responses

| Status | Body |
| --- | --- |
| 201 | `{ "id": "string", "name": "string", "quantity": 1 }` — `quantity` is an **integer**, not a string |
| 422 | Validation error (invalid name/quantity) |
| 409 | `{ "detail": "item already exists" }` when name collides |

### Notes

- Idempotency is not required for this example.
- Persist only if the project already has a store; otherwise in-memory is acceptable for the demo.
