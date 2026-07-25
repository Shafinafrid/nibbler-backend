from datetime import date as date_cls, datetime
from typing import Any, List, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── Notes ────────────────────────────────────────────────────────────────────

class NoteIn(BaseModel):
    id: Optional[str] = None          # client-generated; server mints one if absent
    book_id: str
    book_title: Optional[str] = None
    book_color: Optional[str] = None
    card_index: int
    card_eyebrow: Optional[str] = None
    card_title: Optional[str] = None
    card_body: Optional[str] = None
    text: str


class NoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    book_id: str
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
    id: Optional[str] = None
    book_id: str
    book_title: Optional[str] = None
    book_color: Optional[str] = None
    card_index: int
    card_eyebrow: Optional[str] = None
    card_title: Optional[str] = None
    card_body: Optional[str] = None


class HighlightOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    book_id: str
    book_title: Optional[str] = None
    book_color: Optional[str] = None
    card_index: int
    card_eyebrow: Optional[str] = None
    card_title: Optional[str] = None
    card_body: Optional[str] = None
    created_at: Optional[datetime] = None


# ── Chat ─────────────────────────────────────────────────────────────────────

class ChatMessageIn(BaseModel):
    id: Optional[str] = None
    book_id: str
    role: str
    content: str
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
    id: Optional[str] = None
    book_id: str
    completed_date: date_cls
    read_length: Optional[int] = None


class CompletionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    book_id: str
    completed_date: date_cls
    read_length: Optional[int] = None


# ── Settings / state ─────────────────────────────────────────────────────────

class SettingsIn(BaseModel):
    """Every field optional: the app PATCHes just what changed, so a client
    that doesn't know about a newer setting can never blank it."""
    read_length: Optional[int] = None
    delivery_hour: Optional[int] = None
    delivery_minute: Optional[int] = None
    daily_quiz: Optional[bool] = None
    streak_alerts: Optional[bool] = None
    dark_mode: Optional[bool] = None
    sound_enabled: Optional[bool] = None
    active_book_ids: Optional[List[str]] = None
    inactive_order: Optional[List[str]] = None


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
    review_state: Optional[Any] = None
    quiz_attempts: Optional[int] = None
    quiz_correct: Optional[int] = None


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
