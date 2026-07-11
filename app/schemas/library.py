from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional


class LibraryItemCreate(BaseModel):
    title: str = Field(..., max_length=300)
    type: str   # pdf | url | text | note
    # Same ceiling as extracted PDF/URL text (settings.max_extracted_text_chars)
    content: Optional[str] = Field(None, max_length=2_000_000)
    mode: Optional[str] = "wisdom"          # wisdom | story
    kind: Optional[str] = "book"            # book | article | paper
    author: Optional[str] = None
    growth_profile_name: Optional[str] = None


class LibraryItemUrlCreate(BaseModel):
    url: str = Field(..., max_length=2000)
    title: Optional[str] = Field(None, max_length=300)
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
    is_active: Optional[bool] = True
    created_at: datetime

    class Config:
        from_attributes = True


class SetActiveRequest(BaseModel):
    active: bool


class LibraryItemList(BaseModel):
    items: list[LibraryItemResponse]
    total: int
    limit_reached: bool
