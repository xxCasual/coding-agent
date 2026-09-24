from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()
_STORE: dict[str, dict] = {}


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1)
    priority: str | None = None


class Item(BaseModel):
    id: str
    name: str
    # Bug: response quantity is serialized as string.
    quantity: str
    priority: str | None = None


@app.get("/health")
def health() -> dict:
    return {"ok": False, "service": "items"}


@app.post("/items", response_model=Item, status_code=201)
def create_item(payload: ItemCreate) -> Item:
    item_id = "item-1"
    record = {
        "id": item_id,
        "name": payload.name,
        "quantity": str(payload.quantity),
        "priority": payload.priority,
    }
    _STORE[item_id] = record
    return Item(**record)


@app.get("/items")
def list_items() -> dict:
    return {"items": None}
