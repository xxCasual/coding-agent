import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
client.post("/items", json={"name": "listed", "quantity": 1})
response = client.get("/items")
assert response.status_code == 200
payload = response.json()["items"]
assert isinstance(payload, list)
assert any(item.get("name") == "listed" for item in payload)

