import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from dal import NotesDal
from service import NotesService
service = NotesService(NotesDal())
note = service.create("alice", "a", "")
assert service.delete("bob", note.id) is False
assert service.get("alice", note.id) is not None

