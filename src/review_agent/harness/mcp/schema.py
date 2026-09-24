from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

UNSUPPORTED_SCHEMA_KEYS = frozenset(
    {
        "$dynamicRef",
        "$dynamicAnchor",
        "$recursiveRef",
        "unevaluatedProperties",
        "unevaluatedItems",
    }
)


class SchemaCapabilityError(ValueError):
    """JSON Schema cannot be enforced with the project's validator subset."""


class InvalidJsonArguments(ValueError):
    """Instance failed JSON Schema validation."""


def schema_capability_gap(schema: Any) -> str | None:
    if schema is True or schema == {}:
        return "input schema is empty or unconstrained"
    if not isinstance(schema, dict):
        return "input schema must be a JSON object"
    found: list[str] = []
    _walk_schema(schema, "$", found, seen_refs=set())
    if found:
        return "unsupported JSON Schema features: " + "; ".join(found)
    return None


def validate_json_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> dict[str, Any]:
    gap = schema_capability_gap(schema)
    if gap:
        raise SchemaCapabilityError(gap)
    try:
        validator = Draft202012Validator(schema)
        validator.check_schema(schema)
    except SchemaError as exc:
        raise SchemaCapabilityError(f"input schema is not a usable JSON Schema: {exc.message}") from exc
    errors = sorted(validator.iter_errors(arguments), key=lambda err: list(err.path))
    if not errors:
        return dict(arguments)
    parts = [_format_jsonschema_error(error) for error in errors]
    raise InvalidJsonArguments("Invalid tool arguments: " + "; ".join(parts))


def _format_jsonschema_error(error: ValidationError) -> str:
    loc = ".".join(str(part) for part in error.absolute_path) or "(root)"
    return f"{loc}: {error.message}"


def _walk_schema(node: Any, path: str, found: list[str], *, seen_refs: set[str]) -> None:
    if isinstance(node, list):
        for index, item in enumerate(node):
            _walk_schema(item, f"{path}[{index}]", found, seen_refs=seen_refs)
        return
    if not isinstance(node, dict):
        return
    for key in UNSUPPORTED_SCHEMA_KEYS:
        if key in node:
            found.append(f"{path}: {key}")
    ref = node.get("$ref")
    if isinstance(ref, str):
        if ref in seen_refs:
            found.append(f"{path}: recursive $ref")
        elif not ref.startswith("#"):
            found.append(f"{path}: non-local $ref")
    for key, value in node.items():
        if key == "$ref":
            continue
        _walk_schema(value, f"{path}.{key}", found, seen_refs=seen_refs)
