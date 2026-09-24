from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Any

REGISTERED_CONTRACTS = {
    "items-v1": "items-v1.json",
    "items-v2": "items-v2.json",
    "items-unsupported": "items-unsupported.json",
}


class ContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def catalog_root() -> Path:
    env = os.environ.get("REVIEW_AGENT_CONTRACT_ROOT")
    if env:
        root = Path(env).expanduser().resolve()
        if not root.is_dir():
            raise ContractError("contract_catalog_missing", f"contract catalog is not a directory: {root}")
        return root
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "examples" / "contracts"
        if candidate.is_dir():
            return candidate
    raise ContractError("contract_catalog_missing", "no registered contract catalog found")


def load_contract(contract_id: str) -> tuple[dict[str, Any], str, Path]:
    _reject_path_like(contract_id)
    filename = REGISTERED_CONTRACTS.get(contract_id)
    if filename is None:
        raise ContractError("unknown_contract_id", f"unknown contract_id: {contract_id}")
    path = catalog_root() / filename
    if not path.is_file():
        raise ContractError("unknown_contract_id", f"registered contract file is missing: {contract_id}")
    raw = path.read_bytes()
    try:
        document = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ContractError("invalid_contract", f"contract {contract_id} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise ContractError("invalid_contract", f"contract {contract_id} must be a JSON object")
    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith("3.1"):
        raise ContractError(
            "unsupported_openapi_version",
            f"unsupported OpenAPI version {version!r}; only 3.1 is accepted",
        )
    digest = sha256(raw).hexdigest()
    return document, digest, path


def _reject_path_like(contract_id: str) -> None:
    if not contract_id or contract_id != contract_id.strip():
        raise ContractError("unknown_contract_id", "contract_id is required")
    if any(part in contract_id for part in ("/", "\\", "..", ":")):
        raise ContractError("unknown_contract_id", "contract_id must be a registered id, not a path or URL")
