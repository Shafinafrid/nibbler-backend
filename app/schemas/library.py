from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class LibraryItemCreate(BaseModel):
    title: str
    type: str   # pdf | url | text | note
    content: Optional[str] = None
    mode: Optional[str] = "wisdom"          # wisdom | story
    kind: Optional[str] = "book"            # book | article | paper
    author: Optional[str] = None
    growth_profile_name: Optional[str] = None


class LibraryItemUrlCreate(BaseModel):
    url: str
    title: Optional[str] = None
    mode: Optional[str] = "wisdom"
    kind: Optional[str] = "article"
    growth_profile_name: Optional[str] = None


class LibraryItemResponse(BaseModel):
    id: str
    user_id: str
    title: str
    type: str
    file_url: Optional[str]
    file_size: Optional[int]
    source_url: Optional[str]
    processed: bool
    chunk_count: int
    processing_error: Optional[str]
    mode: Optional[str] = "wisdom"
    kind: Optional[str] = "book"
    author: Optional[str] = None
    growth_profile_name: Optional[str] = None
    story_progress: Optional[int] = 0
    created_at: datetime

    class Config:
        from_attributes = True


class LibraryItemList(BaseModel):
    items: list[LibraryItemResponse]
    total: int
    limit_reached: bool
