import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
import json
from pathlib import Path
from adapter import AdapterError, ChatAdapter, TimeoutError_
payloads = json.loads((root / "fixtures" / "response-fixture.json").read_text(encoding="utf-8"))
adapter = ChatAdapter()
ok = adapter.complete(payloads["success"])
assert ok.usage == {"input_tokens": 10, "output_tokens": 2}
try:
    adapter.complete(payloads["timeout"])
except TimeoutError_ as exc:
    assert exc.usage is None
else:
    raise AssertionError("timeout")
try:
    adapter.complete(payloads["invalid"])
except AdapterError as exc:
    assert exc.code == "invalid_response"
else:
    raise AssertionError("invalid")
try:
    adapter.complete(payloads["auth"])
except AdapterError as exc:
    assert exc.code == "auth"
else:
    raise AssertionError("auth")

