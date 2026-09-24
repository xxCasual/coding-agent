from __future__ import annotations

from pathlib import Path

import pytest

from review_agent.mcp_servers.api_contract.catalog import ContractError, load_contract
from review_agent.mcp_servers.api_contract.logic import (
    compare_contract_versions,
    get_endpoint_contract,
    validate_response_sample,
)

CONTRACTS = Path(__file__).resolve().parents[1] / "examples" / "contracts"


@pytest.fixture(autouse=True)
def _contract_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REVIEW_AGENT_CONTRACT_ROOT", str(CONTRACTS))


def test_get_endpoint_contract_includes_hash_and_local_ref() -> None:
    result = get_endpoint_contract("items-v1", "POST", "/items")
    assert result["contract_id"] == "items-v1"
    assert result["contract_hash"]
    assert result["source"] == "items-v1"
    assert result["request_body"]["content"]["application/json"]["schema"]["required"] == ["name", "quantity"]
    assert "201" in result["responses"]
    assert result["responses"]["201"]["content"]["application/json"]["schema"]["properties"]["quantity"]["type"] == "integer"


@pytest.mark.parametrize(
    ("sample", "expected"),
    [
        ({"id": "1", "name": "widget", "quantity": 2}, "valid"),
        ({"id": "1", "name": "widget", "quantity": "2"}, "invalid"),
        ({"id": "1", "name": "widget"}, "invalid"),
    ],
)
def test_validate_response_sample_types_and_required(sample: dict, expected: str) -> None:
    result = validate_response_sample("items-v1", "POST", "/items", 201, sample)
    assert result["status"] == expected
    assert result["contract_hash"]
    if expected == "invalid":
        assert result["errors"]
    else:
        assert result["errors"] == []


def test_validate_response_sample_enum_and_status() -> None:
    ok = validate_response_sample(
        "items-v1", "POST", "/items", 409, {"detail": "item already exists"}
    )
    assert ok["status"] == "valid"
    bad = validate_response_sample("items-v1", "POST", "/items", 409, {"detail": "nope"})
    assert bad["status"] == "invalid"
    with pytest.raises(ContractError) as exc:
        validate_response_sample("items-v1", "POST", "/items", 404, {"detail": "x"})
    assert exc.value.code == "unknown_status"


def test_validate_unsupported_oneof_is_not_valid() -> None:
    result = validate_response_sample("items-unsupported", "POST", "/items", 201, {"id": "1"})
    assert result["status"] == "unsupported"
    assert result["unsupported"]
    assert result["errors"] == []


def test_compare_contract_versions_detects_type_status_enum() -> None:
    result = compare_contract_versions("items-v1", "items-v2")
    risks = {item["risk"] for item in result["changes"]}
    assert "type_change" in risks
    assert "status_removed" in risks
    assert "enum_change" in risks
    directions = {item["direction"] for item in result["changes"]}
    assert "response" in directions
    assert result["compatibility_claim"] == "detected_changes_only"
    quantity = next(
        item
        for item in result["changes"]
        if item["path"].endswith("/properties/quantity/type")
    )
    assert quantity["old"] == "integer"
    assert quantity["new"] == "string"


def test_unknown_contract_and_path_rejected() -> None:
    with pytest.raises(ContractError) as missing:
        get_endpoint_contract("not-a-contract", "POST", "/items")
    assert missing.value.code == "unknown_contract_id"
    with pytest.raises(ContractError) as path_like:
        get_endpoint_contract("../secrets.json", "POST", "/items")
    assert path_like.value.code == "unknown_contract_id"
    with pytest.raises(ContractError) as endpoint:
        get_endpoint_contract("items-v1", "GET", "/missing")
    assert endpoint.value.code == "unknown_endpoint"


def test_openapi_30_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "old.json").write_text(
        '{"openapi":"3.0.3","info":{"title":"x","version":"1"},"paths":{}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("REVIEW_AGENT_CONTRACT_ROOT", str(tmp_path))
    monkeypatch.setitem(
        __import__("review_agent.mcp_servers.api_contract.catalog", fromlist=["REGISTERED_CONTRACTS"]).REGISTERED_CONTRACTS,
        "old",
        "old.json",
    )
    with pytest.raises(ContractError) as exc:
        load_contract("old")
    assert exc.value.code == "unsupported_openapi_version"
