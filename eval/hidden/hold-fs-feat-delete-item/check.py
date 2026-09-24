import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
created = client.post("/items", json={"name": "gone", "quantity": 1})
item_id = created.json()["id"]
assert client.delete(f"/items/{item_id}").status_code == 204
assert client.delete(f"/items/{item_id}").status_code == 404

