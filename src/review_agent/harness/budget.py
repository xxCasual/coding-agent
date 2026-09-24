"""Small, opt-in CNY ledger guard for bounded DeepSeek Flash evidence runs."""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

from review_agent.config import ModelProfile


class BudgetError(ValueError):
    pass


class BudgetLedger:
    MAX_OUTPUT = 4096
    MAX_BODY_BYTES = 100_000
    RESERVATION = Decimal("0.5")
    LIMIT = Decimal("10")  # Cumulative ceiling explicitly increased by the user; the ledger may be lower.

    def __init__(self, path: str):
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def _locked(self):
        # Separate lock inode survives atomic replacement of the JSON file.
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                data = json.loads(self.path.read_text())
                self._validate(data)
                yield data
            except BudgetError:
                raise
            except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
                raise BudgetError("Budget ledger is missing, invalid, or unavailable; request not authorized.") from exc
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    @staticmethod
    def _money(value):
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise BudgetError("Invalid budget amount")
        return amount

    def _validate(self, data):
        self._money(data["limit_cny"])
        entries = data["requests"]
        if not isinstance(entries, list) or len({item["id"] for item in entries}) != len(entries):
            raise BudgetError("Invalid request ledger")
        for item in entries:
            self._money(item["accounted_cny"])
        for key in ("input", "output"):
            if self._money(data["peak_cny_per_million"][key]) <= 0:
                raise BudgetError("Unknown pricing")

    def _write(self, data):
        fd, name = tempfile.mkstemp(prefix=".budget-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as file:
                json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
                file.write("\n"); file.flush(); os.fsync(file.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def reserve(self, profile: ModelProfile, request: dict) -> str:
        url = urlparse(profile.base_url)
        if (url.scheme != "https" or url.netloc != "api.deepseek.com" or url.path.rstrip("/") not in {"", "/v1"}
                or url.query or url.fragment or profile.model != "deepseek-flash"):
            raise BudgetError("Budget guard supports only the verified official deepseek-flash endpoint.")
        body_bytes = len(json.dumps(request, ensure_ascii=False).encode("utf-8"))
        if body_bytes > self.MAX_BODY_BYTES:
            raise BudgetError("Request too large for the reserved budget.")
        with self._locked() as data:
            prices = data["peak_cny_per_million"]
            if (profile.price_input_per_1m != prices["input"] or profile.price_output_per_1m != prices["output"]
                    or not profile.price_as_of):
                raise BudgetError("Profile prices must match the verified CNY ledger prices.")
            # Conservative text/schema allowance, plus template overhead; never an actual token count.
            upper = ((body_bytes * 2 + 8192) * self._money(prices["input"])
                     + self.MAX_OUTPUT * self._money(prices["output"])) / 1_000_000
            if upper > self.RESERVATION:
                raise BudgetError("Request bound exceeds reservation.")
            used = sum((self._money(item["accounted_cny"]) for item in data["requests"]), Decimal(0))
            if used + self.RESERVATION > min(self.LIMIT, self._money(data["limit_cny"])):
                raise BudgetError("Shared budget exhausted; request was not sent.")
            request_id = uuid.uuid4().hex
            data["requests"].append({"id": request_id, "source": "coding-agent", "model": profile.model,
                "n": 1, "body_bytes": body_bytes, "max_tokens": self.MAX_OUTPUT,
                "accounted_cny": float(self.RESERVATION), "state": "reserved_or_unknown",
                "pricing_date": profile.price_as_of, "peak_cny_per_million": dict(prices),
                "created_at": datetime.now(timezone.utc).isoformat()})
            self._write(data)
            return request_id

    def settle(self, request_id: str, usage, response_model: str | None = None):
        def field(key):
            return usage.get(key) if isinstance(usage, dict) else getattr(usage, key, None)
        prompt, completion = field("prompt_tokens"), field("completion_tokens")
        if any(type(value) is not int or value < 0 for value in (prompt, completion)):
            return  # Missing, interrupted or malformed usage retains the entire reservation.
        with self._locked() as data:
            item = next(item for item in data["requests"] if item["id"] == request_id)
            prices = item["peak_cny_per_million"]
            cost = (prompt * self._money(prices["input"]) + completion * self._money(prices["output"])) / 1_000_000
            item.update(prompt_tokens=prompt, completion_tokens=completion, accounted_cny=float(cost),
                        state="usage_peak_price_upper_estimate", response_model=response_model)
            self._write(data)
