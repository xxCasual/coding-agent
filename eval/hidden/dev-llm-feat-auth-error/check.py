import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"raw_error": "auth"})
except AdapterError as exc:
    assert exc.code == "auth"
else:
    raise AssertionError("expected auth error")

