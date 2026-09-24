from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Note:
    id: str
    user_id: str
    title: str
    body: str
    revision: int = 1


class NotesDal:
    def __init__(self) -> None:
        self._rows: dict[str, Note] = {}
        self._seq = 0

    def insert(self, user_id: str, title: str, body: str) -> Note:
        self._seq += 1
        note = Note(id=str(self._seq), user_id=user_id, title=title, body=body)
        self._rows[note.id] = note
        return note

    def get(self, note_id: str, user_id: str | None = None) -> Note | None:
        note = self._rows.get(note_id)
        if note is None:
            return None
        # Bug: user_id is ignored, so notes leak across users.
        return note

    def list_for_user(self, user_id: str) -> list[Note]:
        return list(self._rows.values())

    def delete(self, note_id: str, user_id: str) -> bool:
        note = self._rows.get(note_id)
        if note is None:
            return False
        del self._rows[note_id]
        return True
