import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
client = TestClient(app)
response = client.post("/items", json={"name": "widget", "quantity": 2})
assert response.status_code == 201, response.text
assert response.json()["quantity"] == 2
assert isinstance(response.json()["quantity"], int)

