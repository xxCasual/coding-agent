import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"raw_error": "weird", "usage": None})
except AdapterError as exc:
    assert exc.code == "other"
    assert exc.usage is None
else:
    raise AssertionError("expected other")

