from fastapi.testclient import TestClient

from app import app

client = TestClient(app)


def test_create_item_returns_integer_quantity():
    response = client.post("/items", json={"name": "widget", "quantity": 2})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "widget"
    assert body["quantity"] == 2
    assert isinstance(body["quantity"], int)
