from __future__ import annotations

"""Scaffold eval hidden checks and manifests. Run from repo root."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HIDDEN = ROOT / "eval" / "hidden"
MANIFESTS = ROOT / "eval" / "manifests"

BUDGET = {"max_steps": 24, "max_verification_repairs": 2}

CHECK_TEMPLATE = '''\
import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
{body}
'''


def write_check(task_id: str, body: str) -> str:
    dest = HIDDEN / task_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "check.py").write_text(CHECK_TEMPLATE.format(body=body.strip() + "\n"), encoding="utf-8")
    return f"eval/hidden/{task_id}/check.py"


def task(
    task_id: str,
    *,
    set_name: str,
    category: str,
    sample: str,
    requirement: str,
    allowed_paths: list[str],
    body: str,
    reviewer_hard: bool = False,
) -> dict:
    hidden = write_check(task_id, body)
    return {
        "task_id": task_id,
        "set": set_name,
        "category": category,
        "sample": sample,
        "tree_hash": "",
        "requirement": requirement,
        "allowed_paths": allowed_paths,
        "budget": BUDGET,
        "hidden_acceptance": {"script": hidden},
        "reviewer_hard": reviewer_hard,
    }


FS = "fastapi-service"
LLM = "llm-adapter"
PB = "python-backend"

DEV = [
    task(
        "dev-fs-fix-quantity",
        set_name="dev",
        category="fix",
        sample=FS,
        requirement="POST /items must return quantity as a JSON integer matching the request body.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
response = client.post("/items", json={"name": "widget", "quantity": 2})
assert response.status_code == 201, response.text
assert response.json()["quantity"] == 2
assert isinstance(response.json()["quantity"], int)
""",
    ),
    task(
        "dev-fs-fix-duplicate-409",
        set_name="dev",
        category="fix",
        sample=FS,
        requirement="Creating two items with the same name must return HTTP 409 with detail 'item already exists'. Distinct names must still succeed.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
first = client.post("/items", json={"name": "dup", "quantity": 1})
second = client.post("/items", json={"name": "dup", "quantity": 1})
other = client.post("/items", json={"name": "other", "quantity": 1})
assert first.status_code == 201, first.text
assert second.status_code == 409, second.text
assert second.json()["detail"] == "item already exists"
assert other.status_code == 201, other.text
""",
    ),
    task(
        "dev-llm-fix-timeout-usage",
        set_name="dev",
        category="fix",
        sample=LLM,
        requirement="Timeouts must surface as a timeout error whose usage is unknown (null), never invented zeros.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import ChatAdapter, TimeoutError_
try:
    ChatAdapter().complete({"raw_error": "timeout", "usage": None})
except TimeoutError_ as exc:
    assert exc.code == "timeout"
    assert exc.usage is None
else:
    raise AssertionError("expected timeout")
""",
    ),
    task(
        "dev-pb-fix-owner-scope",
        set_name="dev",
        category="fix",
        sample=PB,
        requirement="Notes must only be readable by the owning user_id. Another user asking for the same id must get no note.",
        allowed_paths=["dal.py", "service.py"],
        body="""
from dal import NotesDal
from service import NotesService
service = NotesService(NotesDal())
note = service.create("alice", "t", "b")
assert service.get("alice", note.id) is not None
assert service.get("bob", note.id) is None
""",
    ),
    task(
        "dev-fs-feat-get-item",
        set_name="dev",
        category="feature",
        sample=FS,
        requirement="Add GET /items/{id} that returns a previously created item, or 404 when missing.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
created = client.post("/items", json={"name": "solo", "quantity": 3})
assert created.status_code == 201, created.text
item_id = created.json()["id"]
got = client.get(f"/items/{item_id}")
assert got.status_code == 200, got.text
assert got.json()["name"] == "solo"
missing = client.get("/items/does-not-exist")
assert missing.status_code == 404
""",
    ),
    task(
        "dev-llm-feat-auth-error",
        set_name="dev",
        category="feature",
        sample=LLM,
        requirement="Map provider auth failures to a dedicated error code 'auth'. Do not classify them as other.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"raw_error": "auth"})
except AdapterError as exc:
    assert exc.code == "auth"
else:
    raise AssertionError("expected auth error")
""",
    ),
    task(
        "dev-pb-feat-patch-note",
        set_name="dev",
        category="feature",
        sample=PB,
        requirement="Add PATCH /notes/{id} so the owner can update title/body and the revision number increases. Other users must get 404.",
        allowed_paths=["api.py", "service.py", "dal.py"],
        body="""
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
created = client.post("/notes", json={"title": "a", "body": "b"}, headers={"x-user-id": "alice"})
assert created.status_code == 201, created.text
note_id = created.json()["id"]
patched = client.patch(
    f"/notes/{note_id}",
    json={"title": "z", "body": "yy"},
    headers={"x-user-id": "alice"},
)
assert patched.status_code == 200, patched.text
assert patched.json()["title"] == "z"
assert patched.json()["revision"] >= 2
denied = client.patch(
    f"/notes/{note_id}",
    json={"title": "nope"},
    headers={"x-user-id": "bob"},
)
assert denied.status_code == 404
""",
        reviewer_hard=True,
    ),
    task(
        "dev-fs-contract-post-v1",
        set_name="dev",
        category="contract",
        sample=FS,
        requirement="Implement POST /items against contracts/items-v1.json: integer quantity, 409 on duplicate name, 422 on invalid bodies. Read the contract file in the workspace; do not assume MCP is present.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
ok = client.post("/items", json={"name": "c1", "quantity": 4, "priority": "low"})
assert ok.status_code == 201, ok.text
assert isinstance(ok.json()["quantity"], int)
dup = client.post("/items", json={"name": "c1", "quantity": 4})
assert dup.status_code == 409, dup.text
bad = client.post("/items", json={"name": "", "quantity": 1})
assert bad.status_code == 422
""",
        reviewer_hard=True,
    ),
]

HOLD = [
    task(
        "hold-fs-fix-health",
        set_name="heldout",
        category="fix",
        sample=FS,
        requirement="GET /health should report the service as healthy.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
response = TestClient(app).get("/health")
assert response.status_code == 200
assert response.json().get("ok") is True
""",
    ),
    task(
        "hold-fs-fix-unique-ids",
        set_name="heldout",
        category="fix",
        sample=FS,
        requirement="Each successful POST /items must allocate a distinct id.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
ids = [client.post("/items", json={"name": f"n{i}", "quantity": 1}).json()["id"] for i in range(3)]
assert len(set(ids)) == 3
""",
    ),
    task(
        "hold-fs-fix-list-shape",
        set_name="heldout",
        category="fix",
        sample=FS,
        requirement="GET /items must return an object whose items field is a list of created records.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
client.post("/items", json={"name": "listed", "quantity": 1})
response = client.get("/items")
assert response.status_code == 200
payload = response.json()["items"]
assert isinstance(payload, list)
assert any(item.get("name") == "listed" for item in payload)
""",
    ),
    task(
        "hold-fs-fix-priority-enum",
        set_name="heldout",
        category="fix",
        sample=FS,
        requirement="Reject priority values other than low or high on POST /items.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
bad = client.post("/items", json={"name": "p", "quantity": 1, "priority": "urgent"})
assert bad.status_code == 422
ok = client.post("/items", json={"name": "p2", "quantity": 1, "priority": "high"})
assert ok.status_code == 201, ok.text
""",
    ),
    task(
        "hold-fs-feat-delete-item",
        set_name="heldout",
        category="feature",
        sample=FS,
        requirement="Add DELETE /items/{id} that removes an item and returns 204. Missing ids return 404.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
created = client.post("/items", json={"name": "gone", "quantity": 1})
item_id = created.json()["id"]
assert client.delete(f"/items/{item_id}").status_code == 204
assert client.delete(f"/items/{item_id}").status_code == 404
""",
    ),
    task(
        "hold-fs-feat-patch-item",
        set_name="heldout",
        category="feature",
        sample=FS,
        requirement="Add PATCH /items/{id} to update quantity. Missing ids return 404.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
created = client.post("/items", json={"name": "patchme", "quantity": 1})
item_id = created.json()["id"]
patched = client.patch(f"/items/{item_id}", json={"quantity": 9})
assert patched.status_code == 200, patched.text
assert patched.json()["quantity"] == 9
assert client.patch("/items/missing", json={"quantity": 1}).status_code == 404
""",
        reviewer_hard=True,
    ),
    task(
        "hold-fs-feat-name-query",
        set_name="heldout",
        category="feature",
        sample=FS,
        requirement="GET /items?name=... must filter created items by exact name.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
client.post("/items", json={"name": "alpha", "quantity": 1})
client.post("/items", json={"name": "beta", "quantity": 1})
payload = client.get("/items", params={"name": "alpha"}).json()["items"]
assert [item["name"] for item in payload] == ["alpha"]
""",
    ),
    task(
        "hold-fs-contract-v2-priority",
        set_name="heldout",
        category="contract",
        sample=FS,
        requirement="Follow contracts/items-v2.json: POST /items accepts priority low only, quantity remains an integer, and duplicate names are not a 409 in this version.",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
ok = client.post("/items", json={"name": "v2", "quantity": 2, "priority": "low"})
assert ok.status_code == 201, ok.text
assert isinstance(ok.json()["quantity"], int)
high = client.post("/items", json={"name": "v2b", "quantity": 2, "priority": "high"})
assert high.status_code == 422
dup = client.post("/items", json={"name": "v2", "quantity": 2, "priority": "low"})
assert dup.status_code != 409
""",
        reviewer_hard=True,
    ),
    task(
        "hold-fs-contract-get-item",
        set_name="heldout",
        category="contract",
        sample=FS,
        requirement="Expose GET /items/{id} whose 200 body matches the Item schema in contracts/items-v1.json (id, name, integer quantity).",
        allowed_paths=["app.py"],
        body="""
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
created = client.post("/items", json={"name": "getme", "quantity": 5})
item_id = created.json()["id"]
got = client.get(f"/items/{item_id}")
assert got.status_code == 200
body = got.json()
assert set(body) >= {"id", "name", "quantity"}
assert isinstance(body["quantity"], int)
""",
        reviewer_hard=True,
    ),
    task(
        "hold-llm-fix-invalid-code",
        set_name="heldout",
        category="fix",
        sample=LLM,
        requirement="Invalid provider payloads must use error code invalid_response.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"raw_error": "invalid_response"})
except AdapterError as exc:
    assert exc.code == "invalid_response"
else:
    raise AssertionError("expected invalid_response")
""",
    ),
    task(
        "hold-llm-fix-missing-usage",
        set_name="heldout",
        category="fix",
        sample=LLM,
        requirement="When a successful payload omits usage, keep usage unknown instead of writing zeros.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import ChatAdapter
result = ChatAdapter().complete({"content": "ok"})
assert result.content == "ok"
assert result.usage is None
""",
    ),
    task(
        "hold-llm-fix-empty-content",
        set_name="heldout",
        category="fix",
        sample=LLM,
        requirement="A success payload with missing content is invalid_response, not an empty string result.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"usage": {"input_tokens": 1, "output_tokens": 1}})
except AdapterError as exc:
    assert exc.code == "invalid_response"
else:
    raise AssertionError("expected invalid_response")
""",
    ),
    task(
        "hold-llm-fix-unknown-error",
        set_name="heldout",
        category="fix",
        sample=LLM,
        requirement="Unrecognized raw_error values must map to code other and preserve unknown usage.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"raw_error": "weird", "usage": None})
except AdapterError as exc:
    assert exc.code == "other"
    assert exc.usage is None
else:
    raise AssertionError("expected other")
""",
    ),
    task(
        "hold-llm-feat-http-status",
        set_name="heldout",
        category="feature",
        sample=LLM,
        requirement="If the payload includes http_status >= 500, raise code other and attach that status on the error.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"http_status": 503, "content": "nope"})
except AdapterError as exc:
    assert exc.code == "other"
    assert getattr(exc, "http_status", None) == 503
else:
    raise AssertionError("expected other")
""",
    ),
    task(
        "hold-llm-feat-tool-calls-list",
        set_name="heldout",
        category="feature",
        sample=LLM,
        requirement="Successful completions must always return a list for tool_calls, never None.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import ChatAdapter
result = ChatAdapter().complete({"content": "ok", "tool_calls": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
assert result.tool_calls == []
""",
    ),
    task(
        "hold-llm-feat-failover",
        set_name="heldout",
        category="feature",
        sample=LLM,
        requirement="Add complete_with_failover(primary, fallback) that uses fallback when primary raises timeout.",
        allowed_paths=["adapter.py"],
        body="""
from adapter import ChatAdapter
result = ChatAdapter().complete_with_failover(
    {"raw_error": "timeout"},
    {"content": "backup", "usage": {"input_tokens": 1, "output_tokens": 1}},
)
assert result.content == "backup"
""",
        reviewer_hard=True,
    ),
    task(
        "hold-llm-contract-fixture",
        set_name="heldout",
        category="contract",
        sample=LLM,
        requirement="Normalize fixtures/response-fixture.json success, timeout, invalid, and auth entries through the adapter: success keeps usage, failures use the documented error codes and unknown usage.",
        allowed_paths=["adapter.py"],
        body="""
import json
from pathlib import Path
from adapter import AdapterError, ChatAdapter, TimeoutError_
payloads = json.loads((root / "fixtures" / "response-fixture.json").read_text(encoding="utf-8"))
adapter = ChatAdapter()
ok = adapter.complete(payloads["success"])
assert ok.usage == {"input_tokens": 10, "output_tokens": 2}
try:
    adapter.complete(payloads["timeout"])
except TimeoutError_ as exc:
    assert exc.usage is None
else:
    raise AssertionError("timeout")
try:
    adapter.complete(payloads["invalid"])
except AdapterError as exc:
    assert exc.code == "invalid_response"
else:
    raise AssertionError("invalid")
try:
    adapter.complete(payloads["auth"])
except AdapterError as exc:
    assert exc.code == "auth"
else:
    raise AssertionError("auth")
""",
        reviewer_hard=True,
    ),
    task(
        "hold-pb-fix-list-scope",
        set_name="heldout",
        category="fix",
        sample=PB,
        requirement="Listing notes must return only the caller's notes.",
        allowed_paths=["dal.py", "service.py"],
        body="""
from dal import NotesDal
from service import NotesService
service = NotesService(NotesDal())
service.create("alice", "a", "")
service.create("bob", "b", "")
assert [note.title for note in service.list("alice")] == ["a"]
""",
    ),
    task(
        "hold-pb-fix-delete-scope",
        set_name="heldout",
        category="fix",
        sample=PB,
        requirement="Users must not delete another user's note.",
        allowed_paths=["dal.py", "service.py"],
        body="""
from dal import NotesDal
from service import NotesService
service = NotesService(NotesDal())
note = service.create("alice", "a", "")
assert service.delete("bob", note.id) is False
assert service.get("alice", note.id) is not None
""",
    ),
    task(
        "hold-pb-fix-blank-title",
        set_name="heldout",
        category="fix",
        sample=PB,
        requirement="POST /notes with a blank title must return HTTP 422 rather than 500.",
        allowed_paths=["api.py", "service.py"],
        body="""
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
response = client.post("/notes", json={"title": "   ", "body": "x"}, headers={"x-user-id": "alice"})
assert response.status_code == 422
""",
    ),
    task(
        "hold-pb-fix-missing-user-header",
        set_name="heldout",
        category="fix",
        sample=PB,
        requirement="Note routes without x-user-id must return 401.",
        allowed_paths=["api.py"],
        body="""
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
assert client.post("/notes", json={"title": "a"}).status_code == 401
assert client.get("/notes").status_code == 401
""",
    ),
    task(
        "hold-pb-feat-delete-route",
        set_name="heldout",
        category="feature",
        sample=PB,
        requirement="Add DELETE /notes/{id} for the owner (204) and 404 for everyone else.",
        allowed_paths=["api.py", "service.py", "dal.py"],
        body="""
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
created = client.post("/notes", json={"title": "a", "body": ""}, headers={"x-user-id": "alice"})
note_id = created.json()["id"]
assert client.delete(f"/notes/{note_id}", headers={"x-user-id": "bob"}).status_code == 404
assert client.delete(f"/notes/{note_id}", headers={"x-user-id": "alice"}).status_code == 204
""",
    ),
    task(
        "hold-pb-feat-search",
        set_name="heldout",
        category="feature",
        sample=PB,
        requirement="Add GET /notes?q= that returns the caller's notes whose title contains the query.",
        allowed_paths=["api.py", "service.py", "dal.py"],
        body="""
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
client.post("/notes", json={"title": "alpha", "body": ""}, headers={"x-user-id": "alice"})
client.post("/notes", json={"title": "beta", "body": ""}, headers={"x-user-id": "alice"})
client.post("/notes", json={"title": "alpha", "body": ""}, headers={"x-user-id": "bob"})
found = client.get("/notes", params={"q": "alp"}, headers={"x-user-id": "alice"})
assert found.status_code == 200
assert [item["title"] for item in found.json()] == ["alpha"]
""",
        reviewer_hard=True,
    ),
    task(
        "hold-pb-contract-note-shape",
        set_name="heldout",
        category="contract",
        sample=PB,
        requirement="Note JSON must include id, user_id, title, body, and revision. Unknown notes return 404 with detail not found.",
        allowed_paths=["api.py"],
        body="""
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
created = client.post("/notes", json={"title": "shape", "body": "z"}, headers={"x-user-id": "alice"})
body = created.json()
assert created.status_code == 201
assert set(body) >= {"id", "user_id", "title", "body", "revision"}
missing = client.get("/notes/nope", headers={"x-user-id": "alice"})
assert missing.status_code == 404
assert missing.json()["detail"] == "not found"
""",
    ),
]


def main() -> None:
    HIDDEN.mkdir(parents=True, exist_ok=True)
    MANIFESTS.mkdir(parents=True, exist_ok=True)
    assert len(DEV) == 8
    assert len(HOLD) == 24
    cats = {"fix": 0, "feature": 0, "contract": 0}
    for item in HOLD:
        cats[item["category"]] += 1
    assert cats == {"fix": 12, "feature": 8, "contract": 4}, cats
    (MANIFESTS / "dev.json").write_text(json.dumps({"tasks": DEV}, indent=2) + "\n", encoding="utf-8")
    (MANIFESTS / "heldout.json").write_text(json.dumps({"tasks": HOLD}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(DEV)} dev and {len(HOLD)} heldout tasks")


if __name__ == "__main__":
    main()
