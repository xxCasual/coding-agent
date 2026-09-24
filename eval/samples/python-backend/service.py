from __future__ import annotations

from dal import Note, NotesDal


class NotesService:
    def __init__(self, dal: NotesDal | None = None) -> None:
        self.dal = dal or NotesDal()

    def create(self, user_id: str, title: str, body: str) -> Note:
        if not title.strip():
            raise ValueError("title required")
        return self.dal.insert(user_id, title, body)

    def get(self, user_id: str, note_id: str) -> Note | None:
        return self.dal.get(note_id, user_id)

    def list(self, user_id: str) -> list[Note]:
        return self.dal.list_for_user(user_id)

    def delete(self, user_id: str, note_id: str) -> bool:
        return self.dal.delete(note_id, user_id)
