from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List


class LibraryItemCreate(BaseModel):
    title: str = Field(..., max_length=300)
    type: str   # pdf | url | text | note
    # Same ceiling as extracted PDF/URL text (settings.max_extracted_text_chars)
    content: Optional[str] = Field(None, max_length=2_000_000)
    mode: Optional[str] = "wisdom"          # wisdom | story
    kind: Optional[str] = "book"            # book | article | paper
    author: Optional[str] = None
    # Assignment identity (Sep 2026). New clients send `growth_profile_id`;
    # `growth_profile_name` is accepted only for pre-update clients and is
    # re-derived server-side from whichever profile actually resolves.
    growth_profile_id: Optional[str] = None
    growth_profile_name: Optional[str] = None


class LibraryItemUrlCreate(BaseModel):
    url: str = Field(..., max_length=2000)
    title: Optional[str] = Field(None, max_length=300)
    mode: Optional[str] = "wisdom"
    kind: Optional[str] = "article"
    growth_profile_id: Optional[str] = None
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
    # The stable assignment identity, plus its server-derived display name.
    # Clients must key their pickers off the ID — the name is a snapshot that
    # the server re-derives, and two profiles can legitimately share one.
    growth_profile_id: Optional[str] = None
    growth_profile_name: Optional[str] = None
    story_progress: Optional[int] = 0
    is_active: Optional[bool] = True
    ocr_status: Optional[str] = None
    ocr_pages_done: Optional[int] = 0
    ocr_pages_total: Optional[int] = 0
    created_at: datetime
    # ── Task 2 (Aug 2026) — Free lifetime source entitlement ──────────────
    # unlocked: the authoritative, backend-computed effective access right
    #   now — always true while Premium/trial/complimentary; while Free,
    #   true only for the persisted ≤3-item selection. The client must
    #   render/gate off THIS field, never re-derive its own lock logic.
    # preselected: the raw persisted selection flag, independent of current
    #   tier — lets the "choose your 3" picker show what's currently chosen
    #   even while still Premium (when `unlocked` is true for everything).
    unlocked: bool = True
    preselected: bool = False
    # Task 2 remediation (exactly-three chooser honesty, 2nd audit): the
    # RAW provenance of the current selection — 'explicit' (the user picked
    # it via PUT /library/free-selection), 'fallback' (computed
    # deterministically at a real downgrade), 'legacy_fallback' (pre-cutover
    # migration), or None (not currently selected). Lets the chooser UI show
    # "you chose this" vs. "we picked this for you" truthfully instead of
    # presenting every current pick as if the user had actively chosen it.
    selection_kind: Optional[str] = None

    class Config:
        from_attributes = True


class SetActiveRequest(BaseModel):
    active: bool


class FreeSelectionRequest(BaseModel):
    """A currently-entitled user choosing which ≤3 sources should stay
    unlocked after their NEXT downgrade. Rejected while already Free — see
    PUT /library/free-selection."""
    item_ids: List[str] = Field(..., max_length=3)


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

    ── Assignment fields (Sep 2026) ──────────────────────────────────────
    Changing which goal a book serves is a PREMIUM capability, enforced in
    the route (403 `premium_required`), so these two fields are handled
    differently from `title`/`mode`.

    OMISSION and EXPLICIT NULL mean different things here, and `is not None`
    cannot tell them apart — the route inspects `model_fields_set` instead:

      · field omitted        -> leave the assignment unchanged
      · growth_profile_id=null -> RESET to the server-resolved default
                                  profile (a wisdom book must always end up
                                  with a profile; "unassigned" is a
                                  transient bootstrap state, not a choice)
      · growth_profile_id="x"  -> assign to that profile, if the caller owns
                                  it and it is live

    `growth_profile_name` remains accepted for pre-update clients only; the
    stored name is always re-derived from whichever profile resolves.
    """
    title: Optional[str] = Field(default=None, min_length=1, max_length=300)
    mode: Optional[str] = None                    # 'wisdom' | 'story'
    growth_profile_id: Optional[str] = None
    growth_profile_name: Optional[str] = None


class LibraryItemList(BaseModel):
    items: list[LibraryItemResponse]
    total: int
    limit_reached: bool
    # Task 2: authoritative numbers for the client's upload-limit copy —
    # "consumed" is the PERMANENT lifetime count, not len(items).
    successful_sources_total: int = 0
    free_source_limit: int = 3
