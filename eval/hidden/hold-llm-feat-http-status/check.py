import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"http_status": 503, "content": "nope"})
except AdapterError as exc:
    assert exc.code == "other"
    assert getattr(exc, "http_status", None) == 503
else:
    raise AssertionError("expected other")

