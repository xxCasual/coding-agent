import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
client.post("/items", json={"name": "alpha", "quantity": 1})
client.post("/items", json={"name": "beta", "quantity": 1})
payload = client.get("/items", params={"name": "alpha"}).json()["items"]
assert [item["name"] for item in payload] == ["alpha"]

