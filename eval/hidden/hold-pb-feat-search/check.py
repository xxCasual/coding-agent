import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
client.post("/notes", json={"title": "alpha", "body": ""}, headers={"x-user-id": "alice"})
client.post("/notes", json={"title": "beta", "body": ""}, headers={"x-user-id": "alice"})
client.post("/notes", json={"title": "alpha", "body": ""}, headers={"x-user-id": "bob"})
found = client.get("/notes", params={"q": "alp"}, headers={"x-user-id": "alice"})
assert found.status_code == 200
assert [item["title"] for item in found.json()] == ["alpha"]

