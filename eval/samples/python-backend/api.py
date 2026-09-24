from __future__ import annotations

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from service import NotesService

app = FastAPI()
_SERVICE = NotesService()


class NoteIn(BaseModel):
    title: str
    body: str = ""


class NoteOut(BaseModel):
    id: str
    user_id: str
    title: str
    body: str
    revision: int


def _user(x_user_id: str | None) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="missing user")
    return x_user_id


@app.post("/notes", response_model=NoteOut, status_code=201)
def create_note(payload: NoteIn, x_user_id: str | None = Header(default=None)) -> NoteOut:
    note = _SERVICE.create(_user(x_user_id), payload.title, payload.body)
    return NoteOut(**note.__dict__)


@app.get("/notes/{note_id}", response_model=NoteOut)
def get_note(note_id: str, x_user_id: str | None = Header(default=None)) -> NoteOut:
    note = _SERVICE.get(_user(x_user_id), note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="not found")
    return NoteOut(**note.__dict__)


@app.get("/notes", response_model=list[NoteOut])
def list_notes(x_user_id: str | None = Header(default=None)) -> list[NoteOut]:
    return [NoteOut(**note.__dict__) for note in _SERVICE.list(_user(x_user_id))]
