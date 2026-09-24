import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from adapter import ChatAdapter
result = ChatAdapter().complete({"content": "ok", "tool_calls": None, "usage": {"input_tokens": 1, "output_tokens": 1}})
assert result.tool_calls == []

