from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class LibraryItemCreate(BaseModel):
    title: str
    type: str   # pdf | url | text | note
    content: Optional[str] = None


class LibraryItemResponse(BaseModel):
    id: str
    user_id: str
    title: str
    type: str
    content: Optional[str]
    file_url: Optional[str]
    file_size: Optional[int]
    processed: bool
    chunk_count: int
    created_at: datetime

    class Config:
        from_attributes = True


class LibraryItemList(BaseModel):
    items: list[LibraryItemResponse]
    total: int
    limit_reached: bool
