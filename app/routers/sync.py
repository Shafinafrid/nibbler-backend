"""
Account-data sync: the endpoints that make a Nibbler account portable.

Design rules (learned the hard way — a growth profile was permanently
destroyed on 2026-07-25 by a blob-replace sync):

1. **List data is never blob-replaced.** Notes, highlights, chats and
   completions are upserted row by row, keyed by a client-generated id.
   A fresh install with an empty cache therefore CANNOT wipe the server by
   syncing — the worst it can do is nothing. Deletion requires an explicit
   DELETE call, which only ever happens from a real user action.
2. **Single-valued data is PATCHed, not PUT.** Settings arrive as "only the
   fields that changed"; unknown/omitted fields are left alone, so an older
   client can't blank a setting it has never heard of.
3. **GET /sync/all is the restore path** and is the only endpoint the app
   needs on a new device.
"""

import base64
import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.rate_limit import limiter
from app.models.user_data import (
    ChatMessage, Completion, Highlight, Note, UserSettings, UserState,
)
from app.schemas.user_data import (
    AvatarIn, ChatMessageIn, ChatMessageOut, CompletionIn, CompletionOut,
    HighlightIn, HighlightOut, IdentityIn, NoteIn, NoteOut, SettingsIn,
    SettingsOut, StateIn, StateOut, SyncAllOut,
)
from app.services.s3_service import S3Service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/sync", tags=["sync"])

MAX_CHAT_PER_BOOK = 200     # generous; the app keeps 40 locally

# Every /sync write used to carry NO rate limit, while every other write path
# in the API had one — and the outbox retries on every app foreground, so a
# client-side loop bug would have hammered Postgres and S3 unthrottled. These
# are keyed by Firebase uid (see app/rate_limit.py) and set far above real
# usage: a heavy session is tens of writes, not hundreds.
SYNC_WRITE_LIMIT = "600/hour"
SYNC_AVATAR_LIMIT = "30/hour"   # each one is a multi-MB body and an S3 PUT
SYNC_READ_LIMIT = "120/hour"    # /sync/all runs on init, ~hourly per device

# review_state is an opaque client blob, so bound it by serialised size rather
# than by shape. ReviewScreen's real runs are a few KB.
MAX_REVIEW_STATE_BYTES = 256_000


def _settings_row(user: User, db: Session) -> UserSettings:
    row = db.query(UserSettings).filter(UserSettings.user_id == user.id).first()
    if not row:
        row = UserSettings(user_id=user.id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _state_row(user: User, db: Session) -> UserState:
    row = db.query(UserState).filter(UserState.user_id == user.id).first()
    if not row:
        row = UserState(user_id=user.id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# ── Restore ──────────────────────────────────────────────────────────────────

@router.get("/all", response_model=SyncAllOut)
@limiter.limit(SYNC_READ_LIMIT)
def sync_all(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Everything this account owns that the app keeps a local cache of.

    Called by AppContext.init() when a device has no local copy — a reinstall,
    a new phone, or a different platform. Library items, nibbles, saved bites
    and the streak come from their own endpoints and are NOT duplicated here.
    """
    notes = db.query(Note).filter(Note.user_id == current_user.id).all()
    highlights = db.query(Highlight).filter(Highlight.user_id == current_user.id).all()
    # ts is always populated on write (falls back to server time), so ordering
    # by it alone is safe and avoids nulls-last dialect/version differences.
    chats = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == current_user.id)
        .order_by(ChatMessage.ts.asc())
        .all()
    )
    completions = db.query(Completion).filter(Completion.user_id == current_user.id).all()

    avatar_data_url = None
    if current_user.avatar_url:
        # Returned inline rather than as a presigned URL: the app renders the
        # avatar from a data URI already, and inlining means no second network
        # hop (and no expiring link) during first-launch restore.
        try:
            raw = S3Service().download_file(current_user.avatar_url)
            avatar_data_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        except Exception as e:
            logger.warning("avatar fetch failed for %s: %s", current_user.id, e)

    return SyncAllOut(
        notes=notes,
        highlights=highlights,
        chats=chats,
        completions=completions,
        settings=db.query(UserSettings).filter(UserSettings.user_id == current_user.id).first(),
        state=db.query(UserState).filter(UserState.user_id == current_user.id).first(),
        avatar_data_url=avatar_data_url,
    )


# ── Notes ────────────────────────────────────────────────────────────────────

@router.put("/notes", response_model=NoteOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def upsert_note(
    data: NoteIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(Note)
        .filter(
            Note.user_id == current_user.id,
            Note.book_id == data.book_id,
            Note.card_index == data.card_index,
        )
        .first()
    )
    if row:
        for f in ("book_title", "book_color", "card_eyebrow", "card_title", "card_body", "text"):
            setattr(row, f, getattr(data, f))
    else:
        payload = {k: v for k, v in data.model_dump().items() if k != "id"}
        row = Note(**payload, user_id=current_user.id)
        if data.id:
            row.id = data.id
        db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent write from another device won the unique index — take theirs.
        db.rollback()
        row = (
            db.query(Note)
            .filter(
                Note.user_id == current_user.id,
                Note.book_id == data.book_id,
                Note.card_index == data.card_index,
            )
            .first()
        )
        if not row:
            raise
    db.refresh(row)
    return row


@router.delete("/notes/{note_id}")
@limiter.limit(SYNC_WRITE_LIMIT)
def delete_note(
    note_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(Note).filter(Note.user_id == current_user.id, Note.id == note_id).delete()
    db.commit()
    return {"ok": True}


# ── Highlights ───────────────────────────────────────────────────────────────

@router.put("/highlights", response_model=HighlightOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def upsert_highlight(
    data: HighlightIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = (
        db.query(Highlight)
        .filter(
            Highlight.user_id == current_user.id,
            Highlight.book_id == data.book_id,
            Highlight.card_index == data.card_index,
        )
        .first()
    )
    if row:
        # Refresh the snapshotted card text, the same way upsert_note does.
        # This branch used to be a no-op, so a highlight kept the wording of
        # whichever deck first created it — visibly stale once a session was
        # regenerated, and inconsistent with notes for no reason.
        for f in ("book_title", "book_color", "card_eyebrow", "card_title", "card_body"):
            setattr(row, f, getattr(data, f))
        db.commit()
    else:
        payload = {k: v for k, v in data.model_dump().items() if k != "id"}
        row = Highlight(**payload, user_id=current_user.id)
        if data.id:
            row.id = data.id
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            row = (
                db.query(Highlight)
                .filter(
                    Highlight.user_id == current_user.id,
                    Highlight.book_id == data.book_id,
                    Highlight.card_index == data.card_index,
                )
                .first()
            )
            if not row:
                raise
    db.refresh(row)
    return row


@router.delete("/highlights/{highlight_id}")
@limiter.limit(SYNC_WRITE_LIMIT)
def delete_highlight(
    highlight_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(Highlight).filter(
        Highlight.user_id == current_user.id, Highlight.id == highlight_id
    ).delete()
    db.commit()
    return {"ok": True}


# ── Chat ─────────────────────────────────────────────────────────────────────

@router.post("/chat", response_model=ChatMessageOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def append_chat(
    data: ChatMessageIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.id:
        existing = db.query(ChatMessage).filter(
            ChatMessage.user_id == current_user.id, ChatMessage.id == data.id
        ).first()
        if existing:
            return existing          # idempotent re-push after a flaky network

    row = ChatMessage(
        user_id=current_user.id,
        book_id=data.book_id,
        role=data.role,
        content=data.content,
        ts=datetime.utcfromtimestamp(data.ts / 1000) if data.ts else datetime.utcnow(),
    )
    if data.id:
        row.id = data.id
    db.add(row)
    db.commit()
    db.refresh(row)

    # Trim the oldest for this book so one long-running conversation can't grow
    # without bound.
    count = db.query(ChatMessage).filter(
        ChatMessage.user_id == current_user.id, ChatMessage.book_id == data.book_id
    ).count()
    if count > MAX_CHAT_PER_BOOK:
        old = (
            db.query(ChatMessage)
            .filter(ChatMessage.user_id == current_user.id, ChatMessage.book_id == data.book_id)
            .order_by(ChatMessage.ts.asc())
            .limit(count - MAX_CHAT_PER_BOOK)
            .all()
        )
        for m in old:
            db.delete(m)
        db.commit()
    return row


@router.delete("/chat/{book_id}")
@limiter.limit(SYNC_WRITE_LIMIT)
def clear_chat(
    book_id: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.query(ChatMessage).filter(
        ChatMessage.user_id == current_user.id, ChatMessage.book_id == book_id
    ).delete()
    db.commit()
    return {"ok": True}


# ── Completions ──────────────────────────────────────────────────────────────

@router.post("/completions", response_model=CompletionOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def add_completion(
    data: CompletionIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.id:
        existing = db.query(Completion).filter(
            Completion.user_id == current_user.id, Completion.id == data.id
        ).first()
        if existing:
            return existing

    row = Completion(
        user_id=current_user.id,
        book_id=data.book_id,
        completed_date=data.completed_date,
        read_length=data.read_length,
    )
    if data.id:
        row.id = data.id
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ── Settings ─────────────────────────────────────────────────────────────────

@router.patch("/settings", response_model=SettingsOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def patch_settings(
    data: SettingsIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _settings_row(current_user, db)
    # exclude_unset: only fields the client actually sent are touched. Sending
    # `None` for an omitted field would blank real settings on every partial save.
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


# ── State ────────────────────────────────────────────────────────────────────

@router.patch("/state", response_model=StateOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def patch_state(
    data: StateIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _state_row(current_user, db)
    payload = data.model_dump(exclude_unset=True)

    # review_state is an opaque client blob — it can't be schema-validated
    # without freezing ReviewScreen's internals, but leaving it completely
    # unbounded means one buggy client can push an arbitrarily large JSON
    # document into a single row.
    review = payload.get("review_state")
    if review is not None:
        try:
            size = len(json.dumps(review))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="review_state must be JSON-serialisable.")
        if size > MAX_REVIEW_STATE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"review_state is too large ({size} bytes).",
            )

    # Quiz counters only ever go UP. A device that has been offline holds a
    # stale, lower count; letting it write that back would silently erase
    # answers recorded from another device.
    for field, value in payload.items():
        if field in ("quiz_attempts", "quiz_correct") and value is not None:
            setattr(row, field, max(getattr(row, field) or 0, value))
        else:
            setattr(row, field, value)
    db.commit()
    db.refresh(row)
    return row


# ── Identity ─────────────────────────────────────────────────────────────────

@router.patch("/identity")
@limiter.limit(SYNC_WRITE_LIMIT)
def patch_identity(
    data: IdentityIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payload = data.model_dump(exclude_unset=True)
    username = payload.get("username")
    if username:
        username = username.strip()
        taken = (
            db.query(User)
            .filter(User.username == username, User.id != current_user.id)
            .first()
        )
        if taken:
            raise HTTPException(status_code=409, detail="That username is taken.")
        payload["username"] = username

    for field, value in payload.items():
        setattr(current_user, field, value)
    current_user.last_seen_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.put("/avatar")
@limiter.limit(SYNC_AVATAR_LIMIT)
def put_avatar(
    data: AvatarIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store the profile picture in S3 (private bucket) and remember its key.

    The app previously kept the photo ONLY as a base64 data URI in local
    prefs, so reinstalling lost it. One fixed key per user means re-uploading
    replaces the old image instead of orphaning objects in the bucket.
    """
    raw_b64 = data.image_base64.split(",", 1)[-1]   # tolerate a data: prefix
    try:
        blob = base64.b64decode(raw_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not decode that image.")
    if not blob:
        raise HTTPException(status_code=400, detail="Empty image.")

    key = f"{current_user.id}/avatar.jpg"
    try:
        S3Service().upload_file(file_content=blob, filename=key, content_type="image/jpeg")
    except Exception as e:
        logger.error("avatar upload failed for %s: %s", current_user.id, e)
        raise HTTPException(status_code=502, detail="Could not store the image right now.")

    current_user.avatar_url = key
    db.commit()
    return {"ok": True}


@router.delete("/avatar")
@limiter.limit(SYNC_AVATAR_LIMIT)
def delete_avatar(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if current_user.avatar_url:
        try:
            S3Service().delete_file(current_user.avatar_url)
        except Exception:
            pass
        current_user.avatar_url = None
        db.commit()
    return {"ok": True}
