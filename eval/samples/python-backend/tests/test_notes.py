from dal import NotesDal
from service import NotesService


def test_get_is_scoped_to_owner():
    dal = NotesDal()
    service = NotesService(dal)
    note = service.create("alice", "t", "b")
    assert service.get("bob", note.id) is None
    assert service.get("alice", note.id) is not None
