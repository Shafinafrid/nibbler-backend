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
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem, DeletedLibraryItem
from app.rate_limit import limiter
from app.models.bite import DailyBite
from app.models.streak import Streak
from app.models.user_data import (
    ChatMessage, Completion, Highlight, Note, UserSettings, UserState,
)
from app.routers.streak import apply_checkin
from app.schemas.user_data import (
    AvatarIn, ChatMessageIn, ChatMessageOut, CompletionIn, CompletionOut,
    HighlightIn, HighlightOut, IdentityIn, NoteIn, NoteOut,
    SessionCompleteIn, SessionCompleteOut, SettingsIn,
    SettingsOut, StateIn, StateOut, SyncAllOut,
)
from app.services.entitlement_service import is_source_unlocked
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


# ── Source-lock enforcement (Task 2, 3rd-audit remediation #5) ─────────────
#
# Notes/highlights/chat/completions are all keyed by a plain `book_id`
# string with NO foreign key to library_items (see delete_library_item's
# comment on why — nothing here cascades). That means a book_id on one of
# these rows can be in exactly one of three states relative to the CALLER:
#
#   'unlocked'     → a LibraryItem the caller owns, currently unlocked
#                    (Premium/trial, or one of the persisted ≤3 Free picks)
#   'locked'       → a LibraryItem the caller owns, currently locked
#   'foreign'      → a LibraryItem that exists but belongs to someone ELSE —
#                    a real ownership-bypass attempt, never "just legacy"
#   'unattributed' → no LibraryItem exists with this id at all — a
#                    reference/demo book outside the user's Library, a
#                    pre-cutover orphan, or a genuinely unattributable
#                    legacy row. CONSERVATIVE, DOCUMENTED POLICY: treated as
#                    allowed (default-allow), same policy already applied
#                    to legacy rows with no daily_bite_id elsewhere in Task
#                    2 — explicitly NOT "proven unlocked", just "nothing
#                    here can attribute it to any of the account's OWN
#                    sources to lock in the first place".
#
# Writes reject 'locked' and 'foreign'; the restore path (`sync_all`) omits
# 'locked' and 'foreign' rows, keeping 'unlocked' and 'unattributed' ones.
def _book_lock_status(db: Session, user: User, book_id) -> str:
    if not book_id:
        return "unattributed"
    item = db.query(LibraryItem).filter(LibraryItem.id == book_id).first()
    if item is None:
        # Task 13 remediation: a book_id that WAS a real source of this
        # user's, now hard-deleted, must stay rejected exactly like a
        # still-tombstoned item — not fall into the 'unattributed'
        # default-allow meant for ids this table never had an opinion
        # about (see DeletedLibraryItem's docstring).
        was_deleted = (
            db.query(DeletedLibraryItem)
            .filter(DeletedLibraryItem.id == book_id, DeletedLibraryItem.user_id == user.id)
            .first()
        )
        return "locked" if was_deleted else "unattributed"
    if item.user_id != user.id:
        return "foreign"
    if item.deletion_state is not None:
        # Task 2 closeout (Verified Blocker 6): a tombstoned book is gone —
        # treated exactly like 'locked' everywhere this status feeds
        # (refused on write, omitted on restore), never like 'unattributed'
        # (that default-allow policy is for rows this table never had any
        # opinion about, not for a source that WAS here and was deleted).
        return "locked"
    return "unlocked" if is_source_unlocked(user, item) else "locked"


def _resolve_book_lock_map(db: Session, user: User, book_ids: set) -> dict:
    """Batch version of `_book_lock_status` for restore filtering — one
    query for every distinct book_id referenced across notes/highlights/
    chats/completions, instead of one query per row."""
    book_ids = {b for b in book_ids if b}
    if not book_ids:
        return {}
    items = (
        db.query(LibraryItem)
        .filter(LibraryItem.user_id == user.id, LibraryItem.id.in_(book_ids))
        .all()
    )
    by_id = {it.id: it for it in items}
    # Task 13 remediation: batch the same hard-deleted-tombstone check
    # _book_lock_status does — only for ids with no live LibraryItem row,
    # since a live row's own deletion_state already covers the tombstoned-
    # but-not-yet-hard-deleted case below.
    missing_ids = book_ids - by_id.keys()
    deleted_ids = set()
    if missing_ids:
        deleted_ids = {
            row.id for row in db.query(DeletedLibraryItem.id).filter(
                DeletedLibraryItem.user_id == user.id,
                DeletedLibraryItem.id.in_(missing_ids),
            ).all()
        }
    out = {}
    for bid in book_ids:
        it = by_id.get(bid)
        # A row already scoped to THIS user's own notes/highlights/etc can
        # only be 'unattributed' or 'unlocked'/'locked' here — 'foreign'
        # cannot occur for data the user's own account created (the write
        # gate already refused a foreign book_id at creation time), so the
        # binary unattributed/unlocked/locked split is sufficient for reads.
        # A tombstoned item is treated as 'locked' (see _book_lock_status).
        if it is None:
            out[bid] = "locked" if bid in deleted_ids else "unattributed"
        elif it.deletion_state is not None:
            out[bid] = "locked"
        else:
            out[bid] = "unlocked" if is_source_unlocked(user, it) else "locked"
    return out


def _require_unlocked_book(db: Session, user: User, book_id) -> None:
    """Write-path gate: reject a locked or foreign book_id before creating/
    updating source-derived user data. 'foreign' 404s (no confirmation that
    the id exists at all, consistent with rename_library_item elsewhere);
    'locked' 403s with the same source_locked code every other Task 2
    enforcement point uses; 'unattributed'/'unlocked' proceed."""
    status = _book_lock_status(db, user, book_id)
    if status == "foreign":
        raise HTTPException(status_code=404, detail="Item not found.")
    if status == "locked":
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now.",
            },
        )


def _card_scope(model, user_id: str, book_id: str, daily_bite_id, card_index: int):
    """Filters identifying ONE card of ONE session, for notes or highlights.

    `card_index` alone is a position within a single deck (every deck restarts
    at 0), so it only identifies a card once paired with the session it belongs
    to. Rows written before 2026-07-26 have no `daily_bite_id` and cannot be
    attributed to a session after the fact, so they keep the old book-scoped
    key — the `IS NULL` branch is what keeps those rows reachable and stops a
    new session-scoped row from being mistaken for one of them.
    """
    if daily_bite_id:
        return (
            model.user_id == user_id,
            model.daily_bite_id == daily_bite_id,
            model.card_index == card_index,
        )
    return (
        model.user_id == user_id,
        model.book_id == book_id,
        model.daily_bite_id.is_(None),
        model.card_index == card_index,
    )


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

    # Task 2 remediation (3rd audit, sync/restore lock enforcement): a
    # locked source's notes/highlights/chat/completions must not come back
    # on restore, an account switch, or a fresh install — they were
    # reachable through EVERY other source-bearing surface before this,
    # making /sync/all the one remaining side door. Rows stay stored
    # (nothing here deletes anything) and reappear the moment the source is
    # unlocked again — see _resolve_book_lock_map's docstring for the
    # unattributed-row policy.
    lock_map = _resolve_book_lock_map(
        db, current_user,
        {getattr(r, "book_id", None) for r in (*notes, *highlights, *chats, *completions)},
    )
    def _visible(row):
        return lock_map.get(row.book_id, "unattributed") != "locked"
    notes = [r for r in notes if _visible(r)]
    highlights = [r for r in highlights if _visible(r)]
    chats = [r for r in chats if _visible(r)]
    completions = [r for r in completions if _visible(r)]

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
    _require_unlocked_book(db, current_user, data.book_id)
    scope = _card_scope(Note, current_user.id, data.book_id, data.daily_bite_id, data.card_index)
    row = db.query(Note).filter(*scope).first()
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
        row = db.query(Note).filter(*scope).first()
        if not row:
            raise
    db.refresh(row)
    return row


@router.delete("/notes/{note_id}")
@limiter.limit(SYNC_WRITE_LIMIT)
def delete_note(
    note_id: str,
    request: Request,
    book_id: Optional[str] = None,
    card_index: Optional[int] = None,
    daily_bite_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete by id, falling back to the card's natural key.

    Rows are UPSERTED by natural key but were only ever DELETED by id, and the
    two disagree across devices: if two devices independently create a note for
    the same card, device A's id owns the row, and device B's later
    `DELETE /sync/notes/<B-id>` matched nothing, returned `{"ok": true}`, and
    the note quietly came back on the next restore. The optional natural-key
    parameters let the delete find the row whoever created it. Older clients
    send only the id and keep the previous behaviour.
    """
    deleted = db.query(Note).filter(
        Note.user_id == current_user.id, Note.id == note_id
    ).delete()

    if not deleted and book_id is not None and card_index is not None:
        scope = _card_scope(Note, current_user.id, book_id, daily_bite_id, card_index)
        deleted = db.query(Note).filter(*scope).delete()

    db.commit()
    return {"ok": True, "deleted": bool(deleted)}


# ── Highlights ───────────────────────────────────────────────────────────────

@router.put("/highlights", response_model=HighlightOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def upsert_highlight(
    data: HighlightIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_unlocked_book(db, current_user, data.book_id)
    scope = _card_scope(Highlight, current_user.id, data.book_id, data.daily_bite_id, data.card_index)
    row = db.query(Highlight).filter(*scope).first()
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
            row = db.query(Highlight).filter(*scope).first()
            if not row:
                raise
    db.refresh(row)
    return row


@router.delete("/highlights/{highlight_id}")
@limiter.limit(SYNC_WRITE_LIMIT)
def delete_highlight(
    highlight_id: str,
    request: Request,
    book_id: Optional[str] = None,
    card_index: Optional[int] = None,
    daily_bite_id: Optional[str] = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete by id, falling back to the card's natural key — see delete_note.

    This matters more for highlights than for notes: `toggleHighlight` mints a
    fresh `hl-…` id every time a card is favourited, so two devices favouriting
    the same card produce two different ids for what is logically one row.
    """
    deleted = db.query(Highlight).filter(
        Highlight.user_id == current_user.id, Highlight.id == highlight_id
    ).delete()

    if not deleted and book_id is not None and card_index is not None:
        scope = _card_scope(Highlight, current_user.id, book_id, daily_bite_id, card_index)
        deleted = db.query(Highlight).filter(*scope).delete()

    db.commit()
    return {"ok": True, "deleted": bool(deleted)}


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

    _require_unlocked_book(db, current_user, data.book_id)
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

    _require_unlocked_book(db, current_user, data.book_id)
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


# ── Session completion (read receipt + completion + streak, atomically) ──────

@router.post("/session-complete", response_model=SessionCompleteOut)
@limiter.limit(SYNC_WRITE_LIMIT)
def session_complete(
    data: SessionCompleteIn,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Finishing a nibble session — the whole thing, once.

    Replaces three separate calls the app used to make, only one of which was
    durable: the completion row went through the outbox, while `markRead` was
    fire-and-forget and `streak/checkin` was a direct call. Finish a session on
    a plane and the streak was silently never credited, and `read_at` stayed
    NULL — which the scheduler reads as "still unread", so it kept holding that
    nibble and never prepared the next one.

    **Idempotent by the client-generated `id`.** The outbox retries on every
    foreground, so this WILL be replayed; a replay returns the current progress
    and changes nothing.

    One deliberate imprecision, stated rather than hidden: the streak is
    credited when this operation is PROCESSED, not at `completed_date`. An op
    queued offline for two days credits the streak on arrival. Reconstructing
    historical streak state from late arrivals is a much larger change, and
    crediting late is generous rather than harmful — the idempotency key is
    what stops it being counted twice.
    """
    existing = db.query(Completion).filter(
        Completion.user_id == current_user.id, Completion.id == data.id
    ).first()

    streak = db.query(Streak).filter(Streak.user_id == current_user.id).first()
    if existing:
        return SessionCompleteOut(
            already_applied=True,
            bite_marked_read=True,
            current_streak=streak.current_streak if streak else 0,
            longest_streak=streak.longest_streak if streak else 0,
            total_bites_read=streak.total_bites_read if streak else 0,
        )

    # Task 2 remediation (3rd audit, sync/restore lock enforcement): a NEW
    # completion (idempotent replays of an ALREADY-applied one already
    # returned above, reporting historical fact, not granting new credit)
    # against a locked or foreign source must not grant a read receipt,
    # completion row, or streak credit — this is the offline-outbox path
    # that could otherwise earn all three for a source that's since been
    # downgraded out of, entirely bypassing every other Task 2 gate.
    _require_unlocked_book(db, current_user, data.book_id)

    # A daily_bite_id must actually belong to the book_id it's submitted
    # with — the ORIGINAL gap this closes: nothing previously checked that
    # the two matched, so a stale/crafted pair could mark a DIFFERENT
    # book's bite read while claiming credit for a book the caller wants
    # unlocked in the response. Bites without a library_item_id at all
    # (demo/reference sessions) can't be cross-checked and are left alone —
    # the same documented "can't attribute, so don't invent a mismatch"
    # policy as everywhere else in this file.
    if data.daily_bite_id:
        bite_for_check = db.query(DailyBite).filter(
            DailyBite.id == data.daily_bite_id,
            DailyBite.user_id == current_user.id,
        ).first()
        if bite_for_check and bite_for_check.library_item_id and bite_for_check.library_item_id != data.book_id:
            raise HTTPException(
                status_code=400,
                detail="daily_bite_id does not belong to the submitted book_id.",
            )

    db.add(Completion(
        id=data.id,
        user_id=current_user.id,
        book_id=data.book_id,
        completed_date=data.completed_date,
        read_length=data.read_length,
    ))

    # Release the scheduler's hold on this nibble. Ownership-scoped, so a
    # client can't mark someone else's bite read.
    marked = False
    if data.daily_bite_id:
        bite = db.query(DailyBite).filter(
            DailyBite.id == data.daily_bite_id,
            DailyBite.user_id == current_user.id,
        ).first()
        if bite:
            if bite.read_at is None:
                bite.read_at = datetime.utcnow()
            marked = True

    streak = apply_checkin(db, current_user.id)

    try:
        db.commit()
    except IntegrityError:
        # Two devices replayed the same completion at once — whoever lost the
        # race still sees a correct, already-applied result.
        db.rollback()
        streak = db.query(Streak).filter(Streak.user_id == current_user.id).first()
        return SessionCompleteOut(
            already_applied=True,
            bite_marked_read=True,
            current_streak=streak.current_streak if streak else 0,
            longest_streak=streak.longest_streak if streak else 0,
            total_bites_read=streak.total_bites_read if streak else 0,
        )

    db.refresh(streak)
    return SessionCompleteOut(
        already_applied=False,
        bite_marked_read=marked,
        current_streak=streak.current_streak,
        longest_streak=streak.longest_streak,
        total_bites_read=streak.total_bites_read,
    )


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
