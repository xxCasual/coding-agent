from pathlib import Path

from fastapi.testclient import TestClient

from review_agent.api.app import create_app
from review_agent.services.review_store import ReviewStore


def test_review_store_status_transitions(tmp_path: Path) -> None:
    store = ReviewStore(tmp_path / "reviews.sqlite3")
    review = store.create_review("https://github.com/octo/demo/pull/1")

    store.mark_running(review.review_id)
    store.save_success(review.review_id, [], "# Report")
    loaded = store.get_review(review.review_id)

    assert loaded is not None
    assert loaded.status == "succeeded"
    assert loaded.final_report == "# Report"


def test_api_health_and_unknown_review(tmp_path: Path) -> None:
    client = TestClient(create_app(ReviewStore(tmp_path / "reviews.sqlite3")))

    assert client.get("/api/health").json() == {"status": "ok"}
    response = client.get("/api/reviews/missing")
    assert response.status_code == 404


def test_frontend_assets_are_served(tmp_path: Path) -> None:
    client = TestClient(create_app(ReviewStore(tmp_path / "reviews.sqlite3")))

    index_response = client.get("/")
    assert index_response.status_code == 200
    assert "text/html" in index_response.headers["content-type"]
    assert "Coding Agent" in index_response.text

    css_response = client.get("/static/styles.css")
    assert css_response.status_code == 200
    assert "text/css" in css_response.headers["content-type"]

    js_response = client.get("/static/app.js")
    assert js_response.status_code == 200
    assert "javascript" in js_response.headers["content-type"]


def test_frontend_exposes_coding_console_controls(tmp_path: Path) -> None:
    client = TestClient(create_app(ReviewStore(tmp_path / "reviews.sqlite3")))

    index_response = client.get("/")
    js_response = client.get("/static/app.js")

    assert index_response.status_code == 200
    assert "id=\"review-root\"" in index_response.text
    assert js_response.status_code == 200
    assert "composer" in js_response.text
    assert "mode-controls" in js_response.text
    assert "/api/sessions" in js_response.text
    assert "/api/runs/" in js_response.text


def test_frontend_script_uses_task_service_routes(tmp_path: Path) -> None:
    client = TestClient(create_app(ReviewStore(tmp_path / "reviews.sqlite3")))

    js_response = client.get("/static/app.js")

    assert js_response.status_code == 200
    assert "/api/workspaces" in js_response.text
    assert "Idempotency-Key" in js_response.text
    assert "accept_and_continue" in js_response.text


def test_api_demo_report_returns_success_payload(tmp_path: Path) -> None:
    client = TestClient(create_app(ReviewStore(tmp_path / "reviews.sqlite3")))

    response = client.get("/api/demo-report")

    assert response.status_code == 200
    payload = response.json()
    assert payload["review_id"] == "demo"
    assert payload["thread_id"] == "demo"
    assert payload["status"] == "succeeded"
    assert payload["error"] is None
    assert payload["findings"]
    assert payload["final_report"]


def test_api_create_review_queues_background_job(tmp_path: Path) -> None:
    client = TestClient(create_app(ReviewStore(tmp_path / "reviews.sqlite3")))
    response = client.post("/api/reviews", json={"pr_url": "https://github.com/octo/demo/pull/1"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"queued", "running", "failed", "succeeded"}
    assert payload["thread_id"] == payload["review_id"]
