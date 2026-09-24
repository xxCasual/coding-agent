import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from adapter import AdapterError, ChatAdapter
try:
    ChatAdapter().complete({"usage": {"input_tokens": 1, "output_tokens": 1}})
except AdapterError as exc:
    assert exc.code == "invalid_response"
else:
    raise AssertionError("expected invalid_response")

