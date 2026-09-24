import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
created = client.post("/items", json={"name": "getme", "quantity": 5})
item_id = created.json()["id"]
got = client.get(f"/items/{item_id}")
assert got.status_code == 200
body = got.json()
assert set(body) >= {"id", "name", "quantity"}
assert isinstance(body["quantity"], int)

