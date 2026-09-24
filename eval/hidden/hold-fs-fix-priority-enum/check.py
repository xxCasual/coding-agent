import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
bad = client.post("/items", json={"name": "p", "quantity": 1, "priority": "urgent"})
assert bad.status_code == 422
ok = client.post("/items", json={"name": "p2", "quantity": 1, "priority": "high"})
assert ok.status_code == 201, ok.text

