from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from review_agent.mcp_servers.api_contract.catalog import ContractError, load_contract

UNSUPPORTED_SCHEMA_KEYS = frozenset({"oneOf", "anyOf", "allOf", "not", "$dynamicRef", "$dynamicAnchor"})


def get_endpoint_contract(contract_id: str, method: str, path: str) -> dict[str, Any]:
    document, digest, _path = load_contract(contract_id)
    operation = _require_operation(document, method, path, contract_id)
    return {
        "contract_id": contract_id,
        "contract_version": str(document.get("info", {}).get("version") or ""),
        "contract_hash": digest,
        "source": contract_id,
        "method": method.upper(),
        "path": path,
        "parameters": _materialize(document, operation.get("parameters") or []),
        "request_body": _materialize(document, operation.get("requestBody")),
        "responses": _materialize(document, operation.get("responses") or {}),
    }


def compare_contract_versions(old_contract_id: str, new_contract_id: str) -> dict[str, Any]:
    old_doc, old_hash, _ = load_contract(old_contract_id)
    new_doc, new_hash, _ = load_contract(new_contract_id)
    changes: list[dict[str, Any]] = []
    unsupported: list[dict[str, str]] = []
    old_ops = _index_operations(old_doc)
    new_ops = _index_operations(new_doc)
    for key in sorted(set(old_ops) | set(new_ops)):
        method, path = key
        old_op = old_ops.get(key)
        new_op = new_ops.get(key)
        pointer = f"/paths/{_escape_pointer(path)}/{method.lower()}"
        if old_op is None:
            changes.append(
                {
                    "path": pointer,
                    "direction": "endpoint",
                    "old": None,
                    "new": "present",
                    "risk": "endpoint_added",
                }
            )
            continue
        if new_op is None:
            changes.append(
                {
                    "path": pointer,
                    "direction": "endpoint",
                    "old": "present",
                    "new": None,
                    "risk": "endpoint_removed",
                }
            )
            continue
        _diff_operation(old_doc, new_doc, old_op, new_op, pointer, changes, unsupported)
    return {
        "old_contract_id": old_contract_id,
        "new_contract_id": new_contract_id,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "changes": changes,
        "unsupported": unsupported,
        "compatibility_claim": "detected_changes_only",
    }


def validate_response_sample(
    contract_id: str,
    method: str,
    path: str,
    status_code: int | str,
    sample: Any,
) -> dict[str, Any]:
    document, digest, _ = load_contract(contract_id)
    operation = _require_operation(document, method, path, contract_id)
    status = str(status_code)
    responses = operation.get("responses") or {}
    if not isinstance(responses, dict) or status not in responses:
        known = sorted(str(key) for key in responses) if isinstance(responses, dict) else []
        raise ContractError(
            "unknown_status",
            f"status_code {status} is not in contract {contract_id} {method.upper()} {path}; known={known}",
        )
    schema = _response_schema(document, responses[status])
    unsupported = _unsupported_in_schema(document, schema, "$")
    if unsupported:
        return {
            "status": "unsupported",
            "contract_hash": digest,
            "errors": [],
            "unsupported": unsupported,
        }
    instance = sample
    if isinstance(sample, str):
        import json

        try:
            instance = json.loads(sample)
        except json.JSONDecodeError as exc:
            return {
                "status": "invalid",
                "contract_hash": digest,
                "errors": [{"path": "(root)", "message": f"sample is not valid JSON: {exc}"}],
                "unsupported": [],
            }
    validator = Draft202012Validator(schema or {"type": "object"})
    errors = [
        {"path": ".".join(str(part) for part in error.absolute_path) or "(root)", "message": error.message}
        for error in sorted(validator.iter_errors(instance), key=lambda err: list(err.path))
    ]
    return {
        "status": "invalid" if errors else "valid",
        "contract_hash": digest,
        "errors": errors,
        "unsupported": [],
    }


def _require_operation(document: dict[str, Any], method: str, path: str, contract_id: str) -> dict[str, Any]:
    paths = document.get("paths")
    if not isinstance(paths, dict) or path not in paths:
        known = sorted(str(key) for key in paths) if isinstance(paths, dict) else []
        raise ContractError(
            "unknown_endpoint",
            f"path {path} is not in contract {contract_id}; known={known}",
        )
    item = paths[path] or {}
    if not isinstance(item, dict):
        raise ContractError("unknown_endpoint", f"path {path} is not an object in contract {contract_id}")
    operation = item.get(method.lower())
    if not isinstance(operation, dict):
        known = sorted(key for key in item if key.lower() in {"get", "post", "put", "patch", "delete"})
        raise ContractError(
            "unknown_endpoint",
            f"method {method.upper()} {path} is not in contract {contract_id}; known={known}",
        )
    return operation


def _index_operations(document: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    paths = document.get("paths") or {}
    if not isinstance(paths, dict):
        return indexed
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        for method, operation in item.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                continue
            if isinstance(operation, dict):
                indexed[(method.lower(), str(path))] = operation
    return indexed


def _diff_operation(
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
    old_op: dict[str, Any],
    new_op: dict[str, Any],
    pointer: str,
    changes: list[dict[str, Any]],
    unsupported: list[dict[str, str]],
) -> None:
    old_body = _schema_from_request_body(old_doc, old_op.get("requestBody"))
    new_body = _schema_from_request_body(new_doc, new_op.get("requestBody"))
    _diff_schema(old_doc, new_doc, old_body, new_body, f"{pointer}/requestBody", "request", changes, unsupported)
    old_responses = old_op.get("responses") or {}
    new_responses = new_op.get("responses") or {}
    if isinstance(old_responses, dict) and isinstance(new_responses, dict):
        for status in sorted(set(old_responses) | set(new_responses), key=str):
            old_schema = _response_schema(old_doc, old_responses.get(status)) if status in old_responses else None
            new_schema = _response_schema(new_doc, new_responses.get(status)) if status in new_responses else None
            status_pointer = f"{pointer}/responses/{status}"
            if status not in old_responses:
                changes.append(
                    {
                        "path": status_pointer,
                        "direction": "response",
                        "old": None,
                        "new": "present",
                        "risk": "status_added",
                    }
                )
                continue
            if status not in new_responses:
                changes.append(
                    {
                        "path": status_pointer,
                        "direction": "response",
                        "old": "present",
                        "new": None,
                        "risk": "status_removed",
                    }
                )
                continue
            _diff_schema(
                old_doc,
                new_doc,
                old_schema,
                new_schema,
                status_pointer,
                "response",
                changes,
                unsupported,
            )


def _schema_from_request_body(document: dict[str, Any], request_body: Any) -> dict[str, Any] | None:
    resolved = _resolve_node(document, request_body)
    if not isinstance(resolved, dict):
        return None
    content = resolved.get("content") or {}
    if not isinstance(content, dict):
        return None
    json_body = content.get("application/json") or next(iter(content.values()), None)
    if not isinstance(json_body, dict):
        return None
    schema = json_body.get("schema")
    return _resolve_node(document, schema) if isinstance(schema, dict) else None


def _response_schema(document: dict[str, Any], response: Any) -> dict[str, Any] | None:
    resolved = _resolve_node(document, response)
    if not isinstance(resolved, dict):
        return None
    content = resolved.get("content") or {}
    if not isinstance(content, dict) or not content:
        return {"type": "object"}
    json_body = content.get("application/json") or next(iter(content.values()))
    if not isinstance(json_body, dict):
        return None
    schema = json_body.get("schema")
    return _resolve_node(document, schema) if isinstance(schema, dict) else None


def _diff_schema(
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
    old_schema: dict[str, Any] | None,
    new_schema: dict[str, Any] | None,
    pointer: str,
    direction: str,
    changes: list[dict[str, Any]],
    unsupported: list[dict[str, str]],
) -> None:
    old_resolved = _resolve_node(old_doc, old_schema) if old_schema else None
    new_resolved = _resolve_node(new_doc, new_schema) if new_schema else None
    unsupported.extend(_unsupported_in_schema(old_doc, old_resolved, pointer))
    unsupported.extend(_unsupported_in_schema(new_doc, new_resolved, pointer))
    old_type = (old_resolved or {}).get("type") if isinstance(old_resolved, dict) else None
    new_type = (new_resolved or {}).get("type") if isinstance(new_resolved, dict) else None
    if old_type != new_type:
        changes.append(
            {
                "path": f"{pointer}/type",
                "direction": direction,
                "old": old_type,
                "new": new_type,
                "risk": "type_change",
            }
        )
    old_required = set((old_resolved or {}).get("required") or []) if isinstance(old_resolved, dict) else set()
    new_required = set((new_resolved or {}).get("required") or []) if isinstance(new_resolved, dict) else set()
    for name in sorted(old_required - new_required):
        changes.append(
            {
                "path": f"{pointer}/required/{name}",
                "direction": direction,
                "old": True,
                "new": False,
                "risk": "required_removed",
            }
        )
    for name in sorted(new_required - old_required):
        changes.append(
            {
                "path": f"{pointer}/required/{name}",
                "direction": direction,
                "old": False,
                "new": True,
                "risk": "required_added",
            }
        )
    old_props = (old_resolved or {}).get("properties") or {} if isinstance(old_resolved, dict) else {}
    new_props = (new_resolved or {}).get("properties") or {} if isinstance(new_resolved, dict) else {}
    if isinstance(old_props, dict) and isinstance(new_props, dict):
        for name in sorted(set(old_props) | set(new_props)):
            if name not in old_props:
                changes.append(
                    {
                        "path": f"{pointer}/properties/{name}",
                        "direction": direction,
                        "old": None,
                        "new": "present",
                        "risk": "property_added",
                    }
                )
                continue
            if name not in new_props:
                changes.append(
                    {
                        "path": f"{pointer}/properties/{name}",
                        "direction": direction,
                        "old": "present",
                        "new": None,
                        "risk": "property_removed",
                    }
                )
                continue
            old_prop = _resolve_node(old_doc, old_props[name])
            new_prop = _resolve_node(new_doc, new_props[name])
            old_prop_type = old_prop.get("type") if isinstance(old_prop, dict) else None
            new_prop_type = new_prop.get("type") if isinstance(new_prop, dict) else None
            if old_prop_type != new_prop_type:
                changes.append(
                    {
                        "path": f"{pointer}/properties/{name}/type",
                        "direction": direction,
                        "old": old_prop_type,
                        "new": new_prop_type,
                        "risk": "type_change",
                    }
                )
            old_enum = old_prop.get("enum") if isinstance(old_prop, dict) else None
            new_enum = new_prop.get("enum") if isinstance(new_prop, dict) else None
            if old_enum != new_enum:
                changes.append(
                    {
                        "path": f"{pointer}/properties/{name}/enum",
                        "direction": direction,
                        "old": old_enum,
                        "new": new_enum,
                        "risk": "enum_change",
                    }
                )


def _unsupported_in_schema(document: dict[str, Any], schema: Any, pointer: str) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    _walk_unsupported(document, schema, pointer, found, seen=set())
    return found


def _walk_unsupported(
    document: dict[str, Any],
    node: Any,
    pointer: str,
    found: list[dict[str, str]],
    *,
    seen: set[str],
) -> None:
    node = _resolve_node(document, node, seen=seen)
    if isinstance(node, list):
        for index, item in enumerate(node):
            _walk_unsupported(document, item, f"{pointer}/{index}", found, seen=seen)
        return
    if not isinstance(node, dict):
        return
    for key in UNSUPPORTED_SCHEMA_KEYS:
        if key in node:
            found.append({"path": f"{pointer}/{key}", "reason": f"unsupported OpenAPI construct {key}"})
    ref = node.get("$ref")
    if isinstance(ref, str) and not ref.startswith("#/"):
        found.append({"path": pointer, "reason": "external $ref is unsupported"})
    for key, value in node.items():
        if key == "$ref":
            continue
        _walk_unsupported(document, value, f"{pointer}/{key}", found, seen=seen)


def _resolve_node(document: dict[str, Any], node: Any, *, seen: set[str] | None = None) -> Any:
    seen = seen if seen is not None else set()
    current = node
    while isinstance(current, dict) and "$ref" in current:
        ref = current["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/"):
            raise ContractError("unsupported_ref", f"external or invalid $ref is unsupported: {ref!r}")
        if ref in seen:
            raise ContractError("unsupported_ref", f"recursive $ref is unsupported: {ref}")
        seen.add(ref)
        current = _lookup_pointer(document, ref)
    return current


def _lookup_pointer(document: dict[str, Any], ref: str) -> Any:
    parts = ref[2:].split("/")
    current: Any = document
    for part in parts:
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict) and key in current:
            current = current[key]
            continue
        raise ContractError("invalid_ref", f"unresolved $ref: {ref}")
    return current


def _escape_pointer(path: str) -> str:
    return path.replace("~", "~0").replace("/", "~1")


def _materialize(document: dict[str, Any], node: Any, *, seen: set[str] | None = None) -> Any:
    seen = set() if seen is None else seen
    node = _resolve_node(document, node, seen=set(seen))
    if isinstance(node, list):
        return [_materialize(document, item, seen=set(seen)) for item in node]
    if isinstance(node, dict):
        return {key: _materialize(document, value, seen=set(seen)) for key, value in node.items() if key != "$ref"}
    return _copy_json(node)


def _copy_json(value: Any) -> Any:
    import copy

    return copy.deepcopy(value)
