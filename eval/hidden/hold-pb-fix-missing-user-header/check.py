import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
import api
client = TestClient(api.app)
assert client.post("/notes", json={"title": "a"}).status_code == 401
assert client.get("/notes").status_code == 401

