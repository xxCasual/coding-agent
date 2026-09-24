import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
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

