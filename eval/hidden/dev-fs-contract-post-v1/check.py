import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
ok = client.post("/items", json={"name": "c1", "quantity": 4, "priority": "low"})
assert ok.status_code == 201, ok.text
assert isinstance(ok.json()["quantity"], int)
dup = client.post("/items", json={"name": "c1", "quantity": 4})
assert dup.status_code == 409, dup.text
bad = client.post("/items", json={"name": "", "quantity": 1})
assert bad.status_code == 422

