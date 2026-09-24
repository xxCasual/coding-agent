import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
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

