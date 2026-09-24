import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
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

