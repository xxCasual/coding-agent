import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from adapter import ChatAdapter, TimeoutError_
try:
    ChatAdapter().complete({"raw_error": "timeout", "usage": None})
except TimeoutError_ as exc:
    assert exc.code == "timeout"
    assert exc.usage is None
else:
    raise AssertionError("expected timeout")

