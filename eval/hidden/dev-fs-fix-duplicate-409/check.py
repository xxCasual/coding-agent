import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
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

