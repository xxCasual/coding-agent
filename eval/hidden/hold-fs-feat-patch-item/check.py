import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
created = client.post("/items", json={"name": "patchme", "quantity": 1})
item_id = created.json()["id"]
patched = client.patch(f"/items/{item_id}", json={"quantity": 9})
assert patched.status_code == 200, patched.text
assert patched.json()["quantity"] == 9
assert client.patch("/items/missing", json={"quantity": 1}).status_code == 404

