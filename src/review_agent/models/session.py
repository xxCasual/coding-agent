from typing import Literal

from pydantic import BaseModel, Field


class ReviewSession(BaseModel):
    review_id: str
    thread_id: str
    pr_url: str
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    findings_json: str = "[]"
    final_report: str | None = None
    error: str | None = None
    messages: list[dict[str, str]] = Field(default_factory=list)
