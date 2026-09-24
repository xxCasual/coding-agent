import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
ids = [client.post("/items", json={"name": f"n{i}", "quantity": 1}).json()["id"] for i in range(3)]
assert len(set(ids)) == 3

