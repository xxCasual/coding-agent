import json
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable

from review_agent.models.finding import Finding
from review_agent.models.session import ReviewSession


class ReviewStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def create_review(self, pr_url: str) -> ReviewSession:
        review_id = uuid.uuid4().hex
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO reviews (
                    review_id, thread_id, pr_url, status, findings_json, final_report, error,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, 'queued', '[]', NULL, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (review_id, review_id, pr_url),
            )
        return self.get_review(review_id)  # type: ignore[return-value]

    def mark_running(self, review_id: str) -> None:
        self._update_status(review_id, "running")

    def save_success(
        self, review_id: str, findings: Iterable[Finding | dict], final_report: str
    ) -> None:
        payload = []
        for finding in findings:
            payload.append(finding.model_dump(mode="json") if isinstance(finding, Finding) else finding)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE reviews
                SET status = 'succeeded', findings_json = ?, final_report = ?, error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE review_id = ?
                """,
                (json.dumps(payload), final_report, review_id),
            )

    def save_failed(self, review_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE reviews
                SET status = 'failed', error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE review_id = ?
                """,
                (error, review_id),
            )

    def get_review(self, review_id: str) -> ReviewSession | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT review_id, thread_id, pr_url, status, findings_json, final_report, error
                FROM reviews
                WHERE review_id = ?
                """,
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        return ReviewSession(
            review_id=row["review_id"],
            thread_id=row["thread_id"],
            pr_url=row["pr_url"],
            status=row["status"],
            findings_json=row["findings_json"] or "[]",
            final_report=row["final_report"],
            error=row["error"],
        )

    def get_findings(self, review_id: str) -> list[Finding]:
        review = self.get_review(review_id)
        if review is None:
            return []
        return [Finding.model_validate(item) for item in json.loads(review.findings_json)]

    def get_final_report(self, review_id: str) -> str:
        review = self.get_review(review_id)
        if review is None:
            return ""
        return review.final_report or ""

    def _update_status(self, review_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE reviews SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE review_id = ?",
                (status, review_id),
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS reviews (
                    review_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    pr_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    findings_json TEXT NOT NULL,
                    final_report TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
