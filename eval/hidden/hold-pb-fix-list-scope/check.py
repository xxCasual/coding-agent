import os
import sys
from pathlib import Path

root = Path(os.environ["EVAL_WORKSPACE"]).resolve()
sys.path.insert(0, str(root))
from dal import NotesDal
from service import NotesService
service = NotesService(NotesDal())
service.create("alice", "a", "")
service.create("bob", "b", "")
assert [note.title for note in service.list("alice")] == ["a"]

