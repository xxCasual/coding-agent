"""Trusted HTTP acceptance. Never copied into Agent source or passed in prompts."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ["EVAL_WORKSPACE"])
from app import app
from fastapi.testclient import TestClient

client = TestClient(app)
assert client.get("/health").json() == {"status": "ok"}
case = os.environ["R4_CASE"]
if case == "pair":
    for tags, expected in [([" A ", "b", "a", "B"], ["a", "b"]), ([" Ä ", "ä", "X"], ["ä", "x"]), (["x"] * 20, ["x"])]:
        response = client.post("/normalize", json={"tags": tags})
        assert response.status_code == 200, response.text
        assert response.json() == {"tags": expected, "count": len(expected)}
    invalid = [{}, {"tags": None}, {"tags": []}, {"tags": ["x"] * 21}, {"tags": [" "]}, {"tags": ["x", ""]}, {"tags": [1]}, {"tags": [True]}, {"tags": "x"}]
    route = "/normalize"
else:
    for values in [[1, 2, -3], [-8, -2], [0], [2] * 20]:
        response = client.post("/sum", json={"values": values})
        expected = {"sum": sum(values), "count": len(values)}
        if case == "followup":
            expected["maximum"] = max(values)
        assert response.status_code == 200, response.text
        assert response.json() == expected, response.text
    invalid = [{}, {"values": None}, {"values": []}, {"values": [1] * 21}, {"values": [True]}, {"values": [1.2]}, {"values": ["1"]}, {"values": "1"}]
    route = "/sum"
for payload in invalid:
    response = client.post(route, json=payload)
    assert response.status_code == 422, (payload, response.status_code, response.text)
print(f"{case}: trusted HTTP acceptance passed")
