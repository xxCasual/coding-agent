from fastapi import FastAPI
from pydantic import BaseModel, Field

app = FastAPI()


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    quantity: int = Field(ge=1)


class Item(BaseModel):
    id: str
    name: str
    # Intentional contract bug: response quantity must be integer.
    quantity: str


@app.post("/items", response_model=Item, status_code=201)
def create_item(payload: ItemCreate) -> Item:
    return Item(id="item-1", name=payload.name, quantity=str(payload.quantity))
