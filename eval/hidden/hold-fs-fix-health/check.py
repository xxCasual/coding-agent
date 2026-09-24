import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from fastapi.testclient import TestClient
from app import app
response = TestClient(app).get("/health")
assert response.status_code == 200
assert response.json().get("ok") is True

