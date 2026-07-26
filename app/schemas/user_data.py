from datetime import date as date_cls, datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Shared bounds. These endpoints take client-supplied strings straight into
# PostgreSQL, and until 2026-07-25 almost none of them were bounded — a buggy
# or hostile client could push arbitrarily large rows. Every limit here is far
# above anything the app can actually produce.
_ID = Field(default=None, max_length=64)
_BOOK_ID = Field(max_length=64)
_TITLE = Field(default=None, max_length=300)
_COLOR = Field(default=None, max_length=32)
_CARD_TEXT = Field(default=None, max_length=20_000)
_CARD_INDEX = Field(ge=0, le=10_000)


# ── Notes ────────────────────────────────────────────────────────────────────

class NoteIn(BaseModel):
    id: Optional[str] = _ID           # client-generated; server mints one if absent
    book_id: str = _BOOK_ID
    # Which SESSION this card belongs to. NULL only on rows written before
    # 2026-07-26 — see app/models/user_data.py for why the key changed.
    daily_bite_id: Optional[str] = _ID
    book_title: Optional[str] = _TITLE
    book_color: Optional[str] = _COLOR
    card_index: int = _CARD_INDEX
    card_eyebrow: Optional[str] = _TITLE
    card_title: Optional[str] = _TITLE
    card_body: Optional[str] = _CARD_TEXT
    text: str = Field(max_length=20_000)


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    book_id: str
    daily_bite_id: Optional[str] = None
    book_title: Optional[str] = None
    book_color: Optional[str] = None
    card_index: int
    card_eyebrow: Optional[str] = None
    card_title: Optional[str] = None
    card_body: Optional[str] = None
    text: str
    updated_at: Optional[datetime] = None


# ── Highlights ───────────────────────────────────────────────────────────────

class HighlightIn(BaseModel):
    id: Optional[str] = _ID
    book_id: str = _BOOK_ID
    daily_bite_id: Optional[str] = _ID
    book_title: Optional[str] = _TITLE
    book_color: Optional[str] = _COLOR
    card_index: int = _CARD_INDEX
    card_eyebrow: Optional[str] = _TITLE
    card_title: Optional[str] = _TITLE
    card_body: Optional[str] = _CARD_TEXT


class HighlightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    book_id: str
    daily_bite_id: Optional[str] = None
    book_title: Optional[str] = None
    book_color: Optional[str] = None
    card_index: int
    card_eyebrow: Optional[str] = None
    card_title: Optional[str] = None
    card_body: Optional[str] = None
    created_at: Optional[datetime] = None


# ── Chat ─────────────────────────────────────────────────────────────────────

class ChatMessageIn(BaseModel):
    id: Optional[str] = _ID
    book_id: str = _BOOK_ID
    # Anything other than these two would render as an unknown bubble in the
    # app and confuse any future prompt that replays this history.
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)
    ts: Optional[int] = None          # client epoch millis


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    book_id: str
    role: str
    content: str
    ts: Optional[datetime] = None


# ── Completions ──────────────────────────────────────────────────────────────

class CompletionIn(BaseModel):
    id: Optional[str] = _ID
    book_id: str = _BOOK_ID
    completed_date: date_cls
    read_length: Optional[int] = Field(default=None, ge=1, le=120)


class CompletionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    book_id: str
    completed_date: date_cls
    read_length: Optional[int] = None


class SessionCompleteIn(BaseModel):
    """Finishing a session: one operation, three effects.

    Previously the app queued only the completion row durably, then fired
    `POST /bites/{id}/read` fire-and-forget and called `POST /streak/checkin`
    directly. Offline, both were lost with no retry: the streak wasn't
    credited, and `read_at` stayed NULL so the server kept holding that nibble.
    """
    id: str = Field(max_length=64)              # client-generated; the idempotency key
    book_id: str = _BOOK_ID
    daily_bite_id: Optional[str] = _ID          # absent for demo/legacy sessions
    completed_date: date_cls
    read_length: Optional[int] = Field(default=None, ge=1, le=120)


class SessionCompleteOut(BaseModel):
    already_applied: bool                       # true when this op was replayed
    bite_marked_read: bool
    current_streak: int
    longest_streak: int
    total_bites_read: int


# ── Settings / state ─────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    """Every field optional: the app PATCHes just what changed, so a client
    that doesn't know about a newer setting can never blank it.

    NB: `None` and "absent" are different here. `patch_settings` uses
    `exclude_unset=True`, so an omitted field is untouched — these bounds only
    apply to fields the client actually sent.
    """
    read_length: Optional[int] = Field(default=None, ge=1, le=120)
    delivery_hour: Optional[int] = Field(default=None, ge=0, le=23)
    delivery_minute: Optional[int] = Field(default=None, ge=0, le=59)
    daily_quiz: Optional[bool] = None
    streak_alerts: Optional[bool] = None
    dark_mode: Optional[bool] = None
    sound_enabled: Optional[bool] = None
    # An empty list is MEANINGFUL (every source switched off) and must stay
    # distinguishable from an omitted field — see restoreFromServer.
    active_book_ids: Optional[List[str]] = Field(default=None, max_length=50)
    inactive_order: Optional[List[str]] = Field(default=None, max_length=500)


class SettingsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    read_length: Optional[int] = None
    delivery_hour: Optional[int] = None
    delivery_minute: Optional[int] = None
    daily_quiz: Optional[bool] = None
    streak_alerts: Optional[bool] = None
    dark_mode: Optional[bool] = None
    sound_enabled: Optional[bool] = None
    active_book_ids: Optional[List[str]] = None
    inactive_order: Optional[List[str]] = None


class StateIn(BaseModel):
    # review_state is an opaque client blob (deck order, position, per-card
    # results). It can't be schema'd without freezing ReviewScreen's internals,
    # but it MUST be bounded — a runaway client could otherwise push an
    # unbounded JSON document into a single row. Enforced in patch_state.
    review_state: Optional[Any] = None
    quiz_attempts: Optional[int] = Field(default=None, ge=0)
    quiz_correct: Optional[int] = Field(default=None, ge=0)


class StateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    review_state: Optional[Any] = None
    quiz_attempts: int = 0
    quiz_correct: int = 0


# ── Identity ─────────────────────────────────────────────────────────────────

class IdentityIn(BaseModel):
    username: Optional[str] = Field(default=None, max_length=32)
    display_name: Optional[str] = Field(default=None, max_length=80)
    timezone: Optional[str] = Field(default=None, max_length=64)
    locale: Optional[str] = Field(default=None, max_length=16)
    platform: Optional[str] = Field(default=None, max_length=16)
    app_version: Optional[str] = Field(default=None, max_length=32)


class AvatarIn(BaseModel):
    # Data URI or bare base64. Capped so a huge upload can't blow up the
    # request; the app already compresses to quality 0.4.
    image_base64: str = Field(max_length=8_000_000)


# ── Restore ──────────────────────────────────────────────────────────────────

class SyncAllOut(BaseModel):
    """Everything a freshly installed app needs to rebuild its local cache."""
    notes: List[NoteOut] = []
    highlights: List[HighlightOut] = []
    chats: List[ChatMessageOut] = []
    completions: List[CompletionOut] = []
    settings: Optional[SettingsOut] = None
    state: Optional[StateOut] = None
    avatar_data_url: Optional[str] = None
