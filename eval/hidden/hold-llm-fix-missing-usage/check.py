import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from adapter import ChatAdapter
result = ChatAdapter().complete({"content": "ok"})
assert result.content == "ok"
assert result.usage is None

