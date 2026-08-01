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
    # Whether the ORIGINAL uploaded file reached S3: stored | failed | None
    # (None = added before this was tracked). `processed` never meant this.
    archive_status: Optional[str] = None
    mode: Optional[str] = "wisdom"
    kind: Optional[str] = "book"
    author: Optional[str] = None
    growth_profile_name: Optional[str] = None
    story_progress: Optional[int] = 0
    is_active: Optional[bool] = True
    ocr_status: Optional[str] = None
    ocr_pages_done: Optional[int] = 0
    ocr_pages_total: Optional[int] = 0
    created_at: datetime

    class Config:
        from_attributes = True


class SetActiveRequest(BaseModel):
    active: bool


class UpdateItemRequest(BaseModel):
    """Edit a source in place. Every field here is FREE to change — none of
    them invalidates any stored work:

      · title — cosmetic.
      · growth_profile_name — never touches the embeddings. Those are vectors
        of the BOOK's text; the profile only shapes the query at
        session-generation time.
      · mode — every processed book already holds BOTH of the things the two
        modes need: item.content (which story mode reads in order) and its
        Pinecone vectors (which wisdom mode retrieves from). index_text runs
        for every upload regardless of mode.

    So switching a book that has been OCR'd costs nothing and needs no
    re-upload — which is the whole point, because OCR is the expensive part.
    """
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    mode: Optional[str] = None                    # 'wisdom' | 'story'
    growth_profile_name: Optional[str] = None


class LibraryItemList(BaseModel):
    items: list[LibraryItemResponse]
    total: int
    limit_reached: bool
