import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
created = client.post("/notes", json={"title": "a", "body": ""}, headers={"x-user-id": "alice"})
note_id = created.json()["id"]
assert client.delete(f"/notes/{note_id}", headers={"x-user-id": "bob"}).status_code == 404
assert client.delete(f"/notes/{note_id}", headers={"x-user-id": "alice"}).status_code == 204

