import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
response = client.post("/notes", json={"title": "   ", "body": "x"}, headers={"x-user-id": "alice"})
assert response.status_code == 422

