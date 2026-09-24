import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
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

