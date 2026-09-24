import json
import logging
import time
from typing import Any

from openai import APIConnectionError, APIError, APITimeoutError, OpenAI

from review_agent.config import Settings, get_settings
from review_agent.errors import ErrorRecord, ReviewerUnavailableError
from review_agent.models.context import ContextBundle
from review_agent.models.diff import DiffHunk
from review_agent.observability import get_logger, log_event
from review_agent.reviewers.finding_parser import finding_from_payload, parse_json_object
from review_agent.reviewers.outcome import ReviewOutcome
from review_agent.reviewers.prompts import HUNK_REVIEW_SYSTEM_PROMPT, HUNK_REVIEW_USER_TEMPLATE

logger = get_logger(__name__)


class DeepSeekLLMReviewer:
    def __init__(
        self,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._client = client

    def review(self, hunk: DiffHunk, context: ContextBundle) -> ReviewOutcome:
        started = time.perf_counter()
        log_event(
            logger,
            logging.INFO,
            "llm.review.start",
            "LLM hunk review started",
            stage="llm.review",
            hunk_id=hunk.hunk_id,
            file_path=hunk.file_path,
        )
        try:
            response = self._client_or_create().chat.completions.create(
                model=self.settings.deepseek_model,
                messages=[
                    {"role": "system", "content": HUNK_REVIEW_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": HUNK_REVIEW_USER_TEMPLATE.format(
                            file_path=hunk.file_path,
                            hunk_id=hunk.hunk_id,
                            change_type=hunk.change_type,
                            added_code=hunk.added_code or "(none)",
                            removed_code=hunk.removed_code or "(none)",
                            context=context.model_dump_json(indent=2),
                        ),
                    },
                ],
                response_format={"type": "json_object"},
                stream=False,
                extra_body={"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
            )
            content = response.choices[0].message.content or '{"findings": []}'
            payload = parse_json_object(content)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            log_event(
                logger,
                logging.INFO,
                "llm.review.success",
                "LLM hunk review succeeded",
                stage="llm.review",
                duration_ms=duration_ms,
                hunk_id=hunk.hunk_id,
                file_path=hunk.file_path,
            )
            findings = [
                finding_from_payload(hunk, item, index)
                for index, item in enumerate(payload.get("findings", []), start=1)
            ]
            return ReviewOutcome(findings=findings)
        except Exception as exc:
            record = self._record_llm_failure(exc)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "LLM hunk review failed; LLM findings will be skipped",
                extra={
                    "event": "llm.review.failure",
                    "stage": "llm.review",
                    "duration_ms": duration_ms,
                    "error_code": record.code,
                    "hunk_id": hunk.hunk_id,
                    "file_path": hunk.file_path,
                },
            )
            return ReviewOutcome(warnings=[record])

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.settings.deepseek_api_key:
            raise ReviewerUnavailableError(
                "DEEPSEEK_API_KEY is required for DeepSeekLLMReviewer",
                public_message="DeepSeek API key is not configured; LLM findings were skipped.",
                code="llm.unavailable",
            )
        try:
            self._client = OpenAI(
                api_key=self.settings.deepseek_api_key,
                base_url=self.settings.deepseek_base_url,
                timeout=self.settings.review_agent_llm_timeout_seconds,
                max_retries=self.settings.review_agent_llm_max_retries,
            )
        except Exception as exc:
            raise ReviewerUnavailableError(
                f"DeepSeek client initialization failed: {exc}",
                public_message="DeepSeek reviewer is unavailable; LLM findings were skipped.",
                metadata={"exception_type": type(exc).__name__},
                code="llm.unavailable",
            ) from exc
        return self._client

    def _record_llm_failure(self, exc: Exception) -> ErrorRecord:
        if isinstance(exc, APITimeoutError | TimeoutError):
            error = ReviewerUnavailableError(
                f"DeepSeek reviewer timed out: {exc}",
                public_message="DeepSeek reviewer timed out; LLM findings were skipped.",
                metadata={"exception_type": type(exc).__name__},
                code="llm.timeout",
            )
        elif isinstance(exc, json.JSONDecodeError):
            error = ReviewerUnavailableError(
                f"DeepSeek reviewer returned invalid JSON: {exc}",
                public_message="DeepSeek reviewer returned invalid JSON; LLM findings were skipped.",
                metadata={"exception_type": type(exc).__name__},
                code="llm.bad_response",
            )
        elif isinstance(exc, APIConnectionError | APIError):
            error = ReviewerUnavailableError(
                f"DeepSeek reviewer API request failed: {exc}",
                public_message="DeepSeek reviewer is unavailable; LLM findings were skipped.",
                metadata={"exception_type": type(exc).__name__},
                code="llm.unavailable",
            )
        elif isinstance(exc, ReviewerUnavailableError):
            error = exc
        else:
            error = ReviewerUnavailableError(
                f"DeepSeek reviewer failed: {exc}",
                public_message="DeepSeek reviewer failed; LLM findings were skipped.",
                metadata={"exception_type": type(exc).__name__},
                code="llm.unavailable",
            )
        return error.to_record(level="warning", stage="llm.review")
