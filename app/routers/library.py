from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Request, Response
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite, SavedBite
from app.models.user_data import ChatMessage, Completion, Highlight, Note
from app.rate_limit import limiter
from app.schemas.library import LibraryItemCreate, LibraryItemResponse, LibraryItemList, LibraryItemUrlCreate, SetActiveRequest, UpdateItemRequest, FreeSelectionRequest
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService, EmbeddingError
from app.services.url_safety import UnsafeUrlError, validate_public_url, fetch_public_url
from app.services.entitlement_service import (
    is_source_unlocked, can_accept_new_upload, finalize_successful_processing,
    free_source_limit, set_explicit_selection, reserve_free_capacity,
    release_reservation, touch_last_active, renew_reservation_lease,
    admit_worker_attempt, renew_worker_attempt, release_worker_attempt,
)
from app.config import get_settings
import logging
import uuid
import threading
from datetime import datetime

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/library", tags=["library"])
settings = get_settings()

MAX_ACTIVE_SOURCES = 5  # mirrors MAX_ACTIVE_BOOKS in nibbler/src/data/sessionStore.js


def _should_start_active(db: Session, user_id: str) -> bool:
    """Whether a NEWLY added item should feed nibbles straight away.

    `LibraryItem.is_active` defaults to True and no upload path used to set it,
    while the 5-source cap was enforced only when toggling a book ON. So a user
    with 8 uploads had 8 rows flagged active server-side, while the Library UI
    (which seeds its list from the server once and slices to 5) showed five on
    and three off. The scheduler reads the flag, so it kept generating nibbles
    from books the user could see were switched off.

    A new upload now joins the line-up only if there is room in it.
    """
    active_count = db.query(LibraryItem).filter(
        LibraryItem.user_id == user_id,
        LibraryItem.is_active.is_(True),
    ).count()
    return active_count < MAX_ACTIVE_SOURCES


def check_upload_limit(user: User, db: Session):
    """Fast-path rejection at request time. Gated on the PERMANENT lifetime
    successful-source counter, not a live row count — a Free user who has
    already consumed all three entitlements cannot upload a replacement by
    deleting an old source first. This check is a courtesy; the authoritative
    enforcement (immune to concurrent-request races) happens at completion
    time in `finalize_successful_processing`."""
    if not can_accept_new_upload(user):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "free_limit_reached",
                "message": (
                    f"Free includes {free_source_limit()} permanent sources, and you've used them "
                    "all — deleting one doesn't free up a new slot. Upgrade to Premium for unlimited uploads."
                ),
                "limit": free_source_limit(),
                "consumed": user.successful_sources_total,
            },
        )


def _to_response(item: LibraryItem, user: User) -> LibraryItemResponse:
    resp = LibraryItemResponse.model_validate(item)
    resp.unlocked = is_source_unlocked(user, item)
    resp.preselected = bool(item.is_unlocked_selection)
    return resp


# ── GET /library/ ─────────────────────────────────────────────────────────────
@router.get("/", response_model=LibraryItemList)
def list_library(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = (
        db.query(LibraryItem)
        .filter(LibraryItem.user_id == current_user.id)
        # A tombstoned item (deletion accepted, external cleanup not yet
        # confirmed complete — see delete_library_item) must disappear from
        # the Library immediately, not linger until cleanup finishes.
        .filter(LibraryItem.deletion_state.is_(None))
        .order_by(LibraryItem.created_at.desc())
        .all()
    )
    count = len(items)
    return LibraryItemList(
        items=[_to_response(i, current_user) for i in items],
        total=count,
        limit_reached=not can_accept_new_upload(current_user),
        successful_sources_total=current_user.successful_sources_total,
        free_source_limit=free_source_limit(),
    )


# ── POST /library/ (plain text / paste) ───────────────────────────────────────
@router.post("/", response_model=LibraryItemResponse)
@limiter.limit("20/hour")
def add_library_item(
    request: Request,
    data: LibraryItemCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    check_upload_limit(current_user, db)

    item = LibraryItem(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        title=data.title,
        type=data.type,
        content=data.content,
        mode=data.mode or "wisdom",
        kind=data.kind or "book",
        author=data.author,
        growth_profile_name=data.growth_profile_name if (data.mode or "wisdom") == "wisdom" else None,
        processed=False,
        is_active=_should_start_active(db, current_user.id),
    )
    touch_last_active(item)
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(process_item_embeddings, item.id, current_user.id)
    return _to_response(item, current_user)


# ── POST /library/upload-pdf ───────────────────────────────────────────────────
@router.post("/upload-pdf", response_model=LibraryItemResponse)
@limiter.limit("10/hour")
def upload_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    title: str = Form(None),
    mode: str = Form("wisdom"),
    kind: str = Form("book"),
    author: str = Form(None),
    growth_profile_name: str = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    check_upload_limit(current_user, db)

    fname = (file.filename or "").lower()
    is_epub = fname.endswith(".epub")
    if not (fname.endswith(".pdf") or is_epub):
        raise HTTPException(status_code=400, detail="Only PDF and EPUB files are supported.")

    max_bytes = settings.max_pdf_upload_mb * 1024 * 1024
    too_large = HTTPException(
        status_code=413,
        detail=f"Files up to {settings.max_pdf_upload_mb} MB are supported — this file is larger.",
    )

    # Everything below used to be able to fail as a bare, undiagnosable 500
    # (Starlette's default handler returns plain text, not JSON — the app has
    # no `detail` to show, so it fell back to a generic "Upload failed" with
    # the real cause thrown away). Found 2026-07-25 after a repeatable EPUB
    # upload failure that couldn't be diagnosed without this.
    try:
        # Fast reject on the declared size, then enforce for real while reading
        # in chunks — one unbounded read() of a huge file can OOM the server.
        if file.size and file.size > max_bytes:
            raise too_large

        chunks, size = [], 0
        # Sync handler (runs in FastAPI's threadpool) — read the spooled temp
        # file via the underlying file object.
        while chunk := file.file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise too_large
            chunks.append(chunk)
        file_content = b"".join(chunks)
        # join() briefly holds TWO full copies of the file; at the 50 MB cap
        # that is 100 MB of the Railway process's RAM per concurrent upload.
        # Drop the chunk list immediately so the peak lasts microseconds, not
        # the whole request.
        chunks.clear()
        if not file_content:
            raise HTTPException(status_code=400, detail="That file appears to be empty.")

        # Respond as soon as the bytes have arrived — S3 archival AND text
        # extraction/embedding all happen in the background task, so the app
        # never waits on Claude, Pinecone, or a slow/broken AWS setup.
        import re as _re
        clean_title = _re.sub(r"\.(pdf|epub)$", "", file.filename or "", flags=_re.IGNORECASE)
        item = LibraryItem(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            title=(title or clean_title).strip(),
            type="epub" if is_epub else "pdf",
            file_url=None,
            file_size=len(file_content),
            mode=mode or "wisdom",
            kind=kind or "book",
            author=author,
            growth_profile_name=growth_profile_name if (mode or "wisdom") == "wisdom" else None,
            processed=False,
            is_active=_should_start_active(db, current_user.id),
        )
        touch_last_active(item)
        db.add(item)
        db.commit()
        db.refresh(item)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("upload_pdf: unexpected failure for user %s (%s)", current_user.id, fname)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)[:300]}") from e

    task = process_epub_embeddings if is_epub else process_pdf_embeddings
    background_tasks.add_task(task, item.id, file_content, current_user.id)
    return _to_response(item, current_user)


# ── POST /library/add-url ──────────────────────────────────────────────────────
@router.post("/add-url", response_model=LibraryItemResponse)
@limiter.limit("10/hour")
def add_url(
    request: Request,
    data: LibraryItemUrlCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Scrape an article/blog URL and add its content to the library."""
    check_upload_limit(current_user, db)

    # SSRF guard: reject non-http(s) schemes and private/internal hosts up
    # front — the background task re-validates every redirect hop too.
    try:
        validate_public_url(data.url)
    except UnsafeUrlError as e:
        raise HTTPException(status_code=400, detail=str(e))

    item = LibraryItem(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        title=data.title or data.url,
        type="url",
        source_url=data.url,
        mode=data.mode or "wisdom",
        kind=data.kind or "article",
        growth_profile_name=data.growth_profile_name if (data.mode or "wisdom") == "wisdom" else None,
        processed=False,
        is_active=_should_start_active(db, current_user.id),
    )
    touch_last_active(item)
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(process_url_embeddings, item.id, data.url, current_user.id)
    return _to_response(item, current_user)


# ── PATCH /library/{item_id}/active ────────────────────────────────────────────
@router.patch("/{item_id}/active", response_model=LibraryItemResponse)
def set_item_active(
    item_id: str,
    data: SetActiveRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle whether this source feeds nibble generation. At most
    MAX_ACTIVE_SOURCES can be active at once (uploads stay uncapped for
    premium — the 5 limit is on ACTIVE sources, swappable anytime)."""
    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == current_user.id,
        LibraryItem.deletion_state.is_(None),  # a tombstoned item is gone
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")

    if data.active and not is_source_unlocked(current_user, item):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now. Upgrade to bring it back into rotation.",
            },
        )

    if data.active and not item.is_active:
        active_count = db.query(LibraryItem).filter(
            LibraryItem.user_id == current_user.id,
            LibraryItem.is_active.is_(True),
        ).count()
        if active_count >= MAX_ACTIVE_SOURCES:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "active_limit_reached",
                    "message": f"You can keep up to {MAX_ACTIVE_SOURCES} sources sending nibbles at a time. Stop one first.",
                    "limit": MAX_ACTIVE_SOURCES,
                },
            )

    item.is_active = data.active
    if data.active:
        touch_last_active(item)
    db.commit()
    db.refresh(item)
    return _to_response(item, current_user)


# ── PUT /library/free-selection ────────────────────────────────────────────────
@router.put("/free-selection", response_model=LibraryItemList)
def set_free_selection(
    data: FreeSelectionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """A currently-entitled user chooses (or changes) which ≤3 owned,
    successfully processed sources should stay unlocked after their NEXT
    Premium/trial expiry (Task 2 requirement: "give the user a clear
    opportunity to choose ... before expiration").

    Requires active entitlement — this is a preference for a FUTURE
    downgrade, not a way to re-shuffle an ALREADY-locked account, which
    would be exactly the "rotate a locked source into an open slot" bypass
    Task 2 explicitly prohibits.
    """
    if not current_user.effective_premium:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "premium_required",
                "message": "Choosing which sources stay unlocked is available while your Premium access is active.",
            },
        )

    limit = free_source_limit()
    ids = list(dict.fromkeys(data.item_ids))  # de-dupe, preserve order
    if len(ids) > limit:
        raise HTTPException(status_code=400, detail=f"Choose at most {limit} sources.")

    if ids:
        valid = db.query(LibraryItem).filter(
            LibraryItem.user_id == current_user.id,
            LibraryItem.id.in_(ids),
            LibraryItem.processed.is_(True),
            LibraryItem.deletion_state.is_(None),  # Task 2 closeout: a tombstoned item cannot be selected
        ).all()
        if len(valid) != len(ids):
            raise HTTPException(
                status_code=400,
                detail="One or more sources are invalid, not yours, or haven't finished processing.",
            )

    set_explicit_selection(db, current_user, set(ids))

    items = (
        db.query(LibraryItem)
        .filter(LibraryItem.user_id == current_user.id)
        .order_by(LibraryItem.created_at.desc())
        .all()
    )
    return LibraryItemList(
        items=[_to_response(i, current_user) for i in items],
        total=len(items),
        limit_reached=not can_accept_new_upload(current_user),
        successful_sources_total=current_user.successful_sources_total,
        free_source_limit=limit,
    )


# ── PATCH /library/{item_id} ──────────────────────────────────────────────────
@router.patch("/{item_id}", response_model=LibraryItemResponse)
def rename_library_item(
    item_id: str,
    data: UpdateItemRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Edit a source: title, mode, or growth profile.

    Everything that makes the item usable — extracted text, chunks, embeddings,
    Pinecone vectors, past nibbles — is keyed by item id and independent of all
    three fields, so none of this needs reprocessing. That matters most for a
    book that had to be OCR'd: getting the mode wrong no longer costs a re-run.

    Ownership is enforced by the same user_id filter every other route here
    uses: a valid token for account A can never rename account B's book.
    """
    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == current_user.id,
        LibraryItem.deletion_state.is_(None),  # a tombstoned item is gone
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")

    # Task 2 final consolidated backend pass (Verified Blocker 7): this
    # generic PATCH used to let a locked (grandfathered-but-over-limit,
    # downgrade-excluded) source have its title/mode/growth-profile
    # changed with no lock check at all — the same bypass every other
    # mutation route here (set_item_active, etc.) already refuses. Any
    # requested field on a locked source is now refused BEFORE any
    # mutation is applied, using the same established 403 shape.
    wants_mutation = (
        data.title is not None or data.mode is not None or data.growth_profile_name is not None
    )
    if wants_mutation and not is_source_unlocked(current_user, item):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now. Upgrade to make changes to it.",
            },
        )

    if data.title is not None:
        title = data.title.strip()
        if not title:
            raise HTTPException(status_code=400, detail="A title cannot be blank.")
        item.title = title

    if data.mode is not None:
        if data.mode not in ("wisdom", "story"):
            raise HTTPException(status_code=400, detail="Mode must be wisdom or story.")
        # Switching INTO story starts the book at page one: a book that has
        # been in wisdom mode has no meaningful sequential position, and
        # carrying over a stale offset would drop the reader mid-chapter.
        if data.mode == "story" and item.mode != "story":
            item.story_progress = 0
        item.mode = data.mode

    if data.growth_profile_name is not None:
        item.growth_profile_name = data.growth_profile_name or None

    db.commit()
    db.refresh(item)
    return _to_response(item, current_user)


# ── Immutable attempt ownership + full-attempt heartbeat (Task 2 lifecycle ──
# remediation, Follow-up 2A) ────────────────────────────────────────────────
#
# Every ingestion worker below (`process_item_embeddings`,
# `process_pdf_embeddings`, `process_epub_embeddings`, `process_url_embeddings`,
# `_run_ocr`) acquires exactly ONE immutable attempt context — item id, user
# id, and either a Free capacity `lease_token` (renewable, reapable) or `None`
# for a Premium-created attempt (no capacity lease, nothing to reap) — and
# retains that EXACT token for its whole lifetime. A worker never re-reads a
# newer token from the mutable `library_items` row and adopts it: a token
# mismatch anywhere means this attempt has been superseded and must stop, not
# retry with whatever the row currently says.
class AttemptOwnershipLost(Exception):
    """Raised the moment a checkpoint (a heartbeat renewal, or an explicit
    `guard.check()`) discovers this attempt's reservation is no longer
    current. Every worker catches this SEPARATELY from a generic Exception:
    the correct response is prompt cancellation and attempt-scoped
    compensating cleanup — never a current-row mutation, a newer attempt's
    release, or another provider/upsert call."""
    def __init__(self, item_id: str, attempt_token: str):
        super().__init__(
            f"attempt {attempt_token} for item {item_id} lost ownership of its reservation"
        )
        self.item_id = item_id
        self.attempt_token = attempt_token


# Deliberately short: a reservation lease lasts RESERVATION_TTL_MINUTES (30)
# once renewed, so a heartbeat every couple of seconds is enormously
# conservative relative to that budget — each renewal is one trivial,
# indexed single-row UPDATE, not remotely "wasteful" for a background
# ingestion job that can genuinely run for minutes. It is deliberately fast
# enough to be observed well inside a few seconds of a worker entering a
# long, otherwise-uncheckpointed provider phase (URL fetch, S3 upload/
# download, chunking + embedding generation, OCR before its first page) —
# the phases with no natural per-operation callback of their own.
_HEARTBEAT_INTERVAL_SECONDS = 2.0


def _atomic_ownership_write(db, item_id: str, user_id: str, attempt_token: str, mutate_fn) -> bool:
    """Task 2 final consolidated backend pass (Verified Blocker 2): the
    ownership predicate and the row write now participate in the SAME
    transaction, on the SAME locked row — never a standalone `SELECT`
    (`_verify_attempt_ownership`) on one session followed by a mutation
    through a DIFFERENT ORM object/session later, which leaves a real
    window for ownership to change in between. Locks the item row
    (`SELECT ... FOR UPDATE`), re-verifies `attempt_token` against the
    freshly-locked row, applies `mutate_fn(locked_item)` ONLY if it still
    matches, and commits — all atomically. Returns whether the mutation
    was applied; `False` means ownership had already changed and the
    write was correctly skipped, never partially applied."""
    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item_id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item or locked_item.last_processing_attempt_id != attempt_token:
        return False
    # Task 2 closeout (Verified Blocker 6): a tombstoned item cannot
    # receive ANY attempt-scoped write, even one presenting a still-
    # matching attempt token — a mid-flight DELETE does not clear
    # last_processing_attempt_id, so the token check alone is not
    # sufficient once deletion has been accepted.
    if locked_item.deletion_state is not None:
        return False
    mutate_fn(locked_item)
    db.commit()
    return True


def _verify_attempt_ownership(db, item_id: str, user_id: str, attempt_token: str) -> bool:
    """Authoritative, real, fresh database read: is `attempt_token` still
    the CURRENT immutable owner of this item's processing? Works
    identically for BOTH tiers — `last_processing_attempt_id` is stamped
    by `admit_worker_attempt` for every tier alike, so this is the one
    ownership signal every attempt has regardless of tier. No row lock is
    taken — this is a pure verification read, never a mutation. Used at
    specific write checkpoints that want a plain boolean rather than an
    exception (e.g. deciding whether it is even safe to attempt a write at
    all); `_AttemptGuard.check()` below is the exception-raising form used
    everywhere else."""
    item = db.query(LibraryItem).filter(LibraryItem.id == item_id, LibraryItem.user_id == user_id).first()
    if not item or item.deletion_state is not None:
        return False
    return item.last_processing_attempt_id == attempt_token


class GuardShutdownFailed(RuntimeError):
    """Raised by `_AttemptGuard.stop()` when its background thread is
    still alive after the bounded join — Task 2 final consolidated
    backend pass, Verified Blocker 2: a worker must never report
    successful shutdown while its own guard thread survives it."""


class _AttemptGuard:
    """The ONE reusable heartbeat/ownership-guard abstraction every
    ingestion worker uses for its complete lifetime, replacing what used to
    be an ad hoc `_heartbeat` closure duplicated (and, for the embedding
    pipelines, silently broken — see `renew_now`) in each worker.

    Task 2 consolidated backend pass: ownership is now checked
    AUTHORITATIVELY against the database at every call to `check()`/
    `checkpoint()` — never merely a cached in-memory flag — because a
    database ownership change landing strictly between two background-
    thread ticks used to be invisible until the next tick actually ran.
    Every ingestion worker calls `check()` immediately before/after each
    external/paid action and before every progress/state/content/file-URL/
    archive/image mutation, so a change that lands in that exact window is
    now caught at the very next checkpoint instead of silently missed.

    Two complementary mechanisms:
      - A background daemon thread renews the lease (Free) / verifies
        attempt ownership (Premium) on `_HEARTBEAT_INTERVAL_SECONDS`,
        through its OWN independent `SessionLocal()` on every tick (never
        the worker's own `db`, and never shared across threads) — this is
        what covers a phase with no natural per-operation checkpoint (the
        worker is genuinely blocked inside one call, e.g. Voyage embedding
        generation or a URL fetch, and nothing NEW can run synchronously
        until that call returns). Runs for EVERY tier now, including
        Premium — a Premium attempt gets the same background ownership
        coverage a Free attempt always had.
      - `renew_now()` — a SYNCHRONOUS, immediate, authoritative check a
        worker calls from its own natural checkpoints (after each Pinecone
        upsert batch, after each OCR page) — the same real-time pattern
        OCR's per-page callback already used correctly; every embedding
        pipeline now shares it too, for both tiers.

    `check()` performs a real, fresh database read every call — no longer
    a cheap in-process flag read — used immediately before/after any
    external call and before every row mutation, so a superseded attempt
    is caught at the closest possible checkpoint rather than only on the
    next background tick.

    A renewal/check that itself RAISES (a database exception) is treated
    as ownership lost — fail closed, never silently assumed still-valid —
    matching every other authoritative check in this module."""

    def __init__(self, item_id: str, user_id: str, lease_token: str, attempt_token: str,
                 interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS):
        self.item_id = item_id
        self.user_id = user_id
        self.lease_token = lease_token
        self.attempt_token = attempt_token
        self.interval_seconds = interval_seconds
        self._lost = threading.Event()
        self._stop = threading.Event()
        self.errors = []
        self._thread = None

    def start(self) -> None:
        # Background heartbeat coverage now starts for EVERY tier —
        # Premium included. There is no capacity lease to renew for
        # Premium, but there IS an immutable attempt id whose ownership
        # can and must still be verified in the background, the same as
        # the free-tier lease is.
        t = threading.Thread(target=self._run, name=f"attempt-guard-{self.item_id}", daemon=True)
        self._thread = t
        t.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.interval_seconds):
                return
            if self._lost.is_set():
                return
            try:
                self.renew_now()
            except Exception as e:  # fail closed — an exception here means
                self.errors.append(e)  # ownership cannot be assumed valid
                self._lost.set()

    def renew_now(self) -> bool:
        """Synchronous, immediate, AUTHORITATIVE ownership verification via
        a fresh session — safe to call from the worker's own thread
        (natural checkpoints) or the background thread. Task 2 final
        consolidated backend pass (Verified Blocker 1): checks BOTH
        concerns, unified, for every tier —
          1. the WORKER-ATTEMPT claim itself (`renew_worker_attempt`,
             `self.attempt_token`) — the one every tier has; and
          2. for a Free attempt ONLY (`self.lease_token is not None`), the
             CAPACITY reservation lease too (`renew_reservation_lease`),
             so an active worker's own capacity slot is never reaped out
             from under it while it is still genuinely live.
        Both must succeed for `ok=True`. Marks ownership lost — and
        re-raises — on any database exception, so a caller can never
        mistake "the check itself failed" for "ownership is still
        valid"."""
        from app.database import SessionLocal
        db = SessionLocal()
        try:
            worker_ok = renew_worker_attempt(db, self.item_id, self.user_id, self.attempt_token)
            if self.lease_token is not None:
                capacity_ok = renew_reservation_lease(db, self.item_id, self.user_id, self.lease_token)
            else:
                capacity_ok = True
            ok = worker_ok and capacity_ok
        except Exception:
            self._lost.set()
            raise
        finally:
            db.close()
        if not ok:
            self._lost.set()
        return ok

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def check(self) -> None:
        """Authoritative: raises AttemptOwnershipLost if the guard already
        knows ownership is gone (a fast path, avoiding a redundant DB
        round-trip immediately after `renew_now()` already set it),
        otherwise performs a REAL, fresh database check right now. Callers
        place this immediately before/after every external/paid action and
        before every row mutation — see the class docstring. A check that
        cannot be completed (a database exception) is treated as ownership
        lost, never silently ignored."""
        if self._lost.is_set():
            raise AttemptOwnershipLost(self.item_id, self.attempt_token)
        try:
            ok = self.renew_now()
        except Exception as e:
            raise AttemptOwnershipLost(self.item_id, self.attempt_token) from e
        if not ok:
            raise AttemptOwnershipLost(self.item_id, self.attempt_token)

    def checkpoint(self, *_args, **_kwargs) -> None:
        """A real, synchronous, authoritative check THAT ALSO raises on
        failure — used as the `on_batch`/OCR-`progress` callback itself
        (both call it with `(done, total)`, hence the tolerant signature),
        so ownership loss stops the loop it's called from immediately (let
        through uncaught by EmbeddingService.index_text/ocr_service's
        caller) rather than being silently discarded."""
        self.check()

    def stop(self) -> None:
        """Task 2 final consolidated backend pass (Verified Blocker 2):
        raises `GuardShutdownFailed` if the background thread is still
        alive after the bounded join — a worker calling this may NOT
        treat a returning `stop()` as proof of clean shutdown while its
        own guard thread survives. Callers must catch this explicitly
        (never let it silently replace an original exception or skip
        their own `db.close()`) — see every pipeline's own `finally`
        block below for the pattern."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise GuardShutdownFailed(
                    f"attempt-guard thread for item {self.item_id!r} is still alive "
                    f"after the bounded join — shutdown cannot be reported as clean"
                )


def _run_ocr(item_id: str, user_id: str, attempt_token: str = None):
    """Read a scanned PDF and push it through the normal indexing path.

    Runs in a BackgroundTask, and ocr_service serialises these so only one
    holds the CPU at a time — see that module for why that guard matters more
    than the cost does.

    Task 2 final consolidated backend pass (Verified Blocker 1): when
    `attempt_token` IS given, it is the exact worker-attempt id `start_ocr`
    already admitted, ATOMICALLY, under a row lock, before this background
    task was ever enqueued — the normal HTTP-triggered path, which is what
    makes two simultaneous OCR requests for the same item enqueue at most
    one worker (a second, racing request's own `admit_worker_attempt` call
    correctly fails and never reaches `background_tasks.add_task` at all).
    This function never re-admits or re-checks capacity in that case —
    that already happened synchronously at the HTTP layer.

    `attempt_token` is OPTIONAL, defaulting to `None`, so this function
    remains a self-sufficient standalone entry point for direct callers
    that never went through `start_ocr` (existing test harnesses) — when
    omitted, it self-reserves capacity and self-admits its own worker
    attempt below, exactly as this function has always done when invoked
    directly.

    Task 2 remediation (renewable reservation lease): OCR is the one
    genuinely long-running, multi-checkpoint paid job in this pipeline — a
    huge scanned book can run well past the default 30-minute reservation
    window. Every page-progress callback renews the lease from THAT moment,
    so active work is never reaped just because the total job duration is
    long; only a job that stops checking in (crashed, killed) goes stale.
    If a checkpoint finds the lease no longer current (reaped and re-
    reserved under a newer attempt's token, or the item was deleted), the
    job aborts immediately rather than continuing to pay for more OCR pages
    under a reservation it no longer owns — cancellation at the one point
    it's locally controllable in this pipeline.
    """
    from app.database import SessionLocal
    from app.services import ocr_service
    db = SessionLocal()
    lease_token = None    # capacity lease — None for a 'premium' attempt; the
                           # ONLY thing finalize_successful_processing/
                           # release_reservation/renew_reservation_lease may
                           # be given (passing the durable attempt id instead
                           # would make finalize incorrectly reject a premium
                           # item — see _release_reservation_after_failure's
                           # docstring).
    guard = None
    try:
        item = db.query(LibraryItem).filter(
            LibraryItem.id == item_id, LibraryItem.user_id == user_id
        ).first()
        if not item:
            if attempt_token is not None:
                release_worker_attempt(db, item_id, user_id, attempt_token)
            return

        if attempt_token is None:
            # Standalone entry point — no prior HTTP-layer admission.
            # Self-reserve capacity, then self-admit this invocation's own
            # worker attempt, exactly as this function has always done
            # when called directly rather than through start_ocr. Mirrors
            # start_ocr's own two outcomes: a capacity rejection is a
            # genuine terminal failure for this call and is reported as
            # such (never left showing a stale 'running'); a worker-
            # admission rejection means a DIFFERENT, live attempt already
            # owns this item, so its in-progress status must not be
            # touched by the loser.
            if not reserve_free_capacity(db, item, user_id):
                item.ocr_status = "failed"
                db.commit()
                return
            db.refresh(item)
            attempt_token = admit_worker_attempt(db, item_id, user_id)
            if attempt_token is None:
                return

        if not item.file_url:
            release_worker_attempt(db, item_id, user_id, attempt_token)
            return

        # Capacity was already reserved and THIS EXACT worker attempt was
        # already admitted (either by start_ocr, or just above) — just
        # read the capacity lease token (None for Premium) back for the
        # guard/finalize calls below.
        lease_token = item.reservation_lease_token

        # `start_ocr` already committed ocr_status="running" atomically
        # alongside admission — nothing to re-set here.

        # Full-attempt heartbeat starts the instant this function begins —
        # covers the S3 download and the pre-first-page OCR phase below,
        # neither of which has a natural per-operation checkpoint of its own.
        guard = _AttemptGuard(item_id, user_id, lease_token, attempt_token)
        guard.start()

        guard.check()
        pdf_bytes = S3Service().download_file(item.file_url)
        guard.check()

        def progress(done: int, total: int):
            # Task 2 consolidated backend pass: ownership is verified
            # BEFORE the write, not after — the row must never show a
            # superseded attempt's page count even for the instant
            # between an unchecked commit and the next checkpoint.
            # `ocr_service.ocr_pdf`'s own per-page handler re-raises ONLY
            # `OcrCancelled` and silently swallows everything else — a
            # raw exception from `renew_now()` (fail-closed on a database
            # error) would otherwise be silently absorbed there instead
            # of aborting the job, so it is explicitly converted here.
            try:
                renewed = guard.renew_now()
            except Exception as e:
                raise ocr_service.OcrCancelled(
                    "This item's ownership could not be verified — aborting OCR."
                ) from e
            if not renewed:
                raise ocr_service.OcrCancelled(
                    "This item's reservation is no longer current — aborting OCR."
                )
            # Committed as it goes so the Library row can show "page 40 of 335"
            # — a job this long with only a spinner reads as broken. Task 2
            # final consolidated backend pass (Verified Blocker 2): the
            # ownership predicate and this write now share ONE transaction
            # via `_atomic_ownership_write` — never a separate renew_now()
            # check followed by a write through a different, unlocked path.
            _atomic_ownership_write(
                db, item_id, user_id, attempt_token,
                lambda locked: (setattr(locked, "ocr_pages_done", done),
                                 setattr(locked, "ocr_pages_total", total)),
            )

        text = ocr_service.ocr_pdf(pdf_bytes, on_progress=progress)
        guard.check()

        chunk_count = EmbeddingService().index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "pdf"},
            attempt_token=attempt_token,
            on_batch=guard.checkpoint,
            check_ownership=guard.check,
        )
        guard.check()
        accepted = finalize_successful_processing(db, item, user_id, chunk_count, lease_token=lease_token, attempt_token=attempt_token)
        if not accepted:
            # Task 2 final consolidated backend pass (Verified Blocker 2):
            # the ownership re-check and this write are now ATOMIC (one
            # locked transaction), not a separate check-then-write — finalize
            # can be rejected precisely BECAUSE this attempt was already
            # superseded, in which case writing `ocr_status="failed"` would
            # overwrite a newer attempt's own, still-current status.
            _atomic_ownership_write(
                db, item_id, user_id, attempt_token,
                lambda locked: setattr(locked, "ocr_status", "failed"),
            )
            logger.info("[ocr] %s rejected/superseded at finalize — compensating cleanup", item_id)
            _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, reason="finalize rejected")
            return
        # Content committed ONLY on this attempt's own confirmed success
        # (3rd-audit remediation #4) — setting it earlier and letting the
        # `not accepted` branch's commit above persist it anyway would let a
        # stale, ultimately-superseded OCR attempt's text silently overwrite
        # a fresher attempt's already-finalized content for the same item.
        # Atomic, ownership-bound write (Verified Blocker 2).
        _atomic_ownership_write(
            db, item_id, user_id, attempt_token,
            lambda locked: (setattr(locked, "content", text),
                             setattr(locked, "ocr_status", "done")),
        )
        logger.info("[ocr] %s indexed %s chunks from %s pages", item_id, chunk_count, item.ocr_pages_total)
    except ocr_service.OcrCancelled as e:
        db.rollback()
        _record_processing_error(item_id, str(e), attempt_token)
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item and item.last_processing_attempt_id == attempt_token:
                item.ocr_status = "failed"
                db.commit()
        except Exception:
            pass
        logger.warning("[ocr] %s aborted — reservation lease lost mid-job", item_id)
        _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, reason=str(e))
    except AttemptOwnershipLost as e:
        db.rollback()
        logger.warning("[ocr] %s aborted — %s", item_id, e)
        _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, reason="ownership lost")
    except Exception as e:
        db.rollback()
        message = str(e)[:400]
        _record_processing_error(item_id, message, attempt_token)
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item and item.last_processing_attempt_id == attempt_token:
                item.ocr_status = "failed"
                db.commit()
            if item:
                # The paid OCR/embedding call failed after capacity was
                # reserved — return the slot rather than stranding it. Safe
                # even if this attempt was superseded: release_reservation
                # itself verifies lease_token before doing anything.
                release_reservation(db, item, user_id, reason=message, lease_token=lease_token)
        except Exception:
            pass
        logger.error("[ocr] FAILED for %s: %s", item_id, e)
        _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, reason=message)
    finally:
        if guard is not None:
            try:
                guard.stop()
            except GuardShutdownFailed as e:
                # Task 2 final consolidated backend pass (Verified Blocker
                # 2): a surviving guard thread is a real operational
                # problem — logged loudly, never silently swallowed — but
                # must not skip this function's own db.close() below.
                logger.error("[attempt-guard] %s", e)
        # Release the worker-attempt claim on EVERY terminal outcome
        # (success or failure) — idempotent (a no-op if this attempt is no
        # longer the current owner) — so a legitimate future retry (the
        # user tapping "read with OCR" again) can be admitted. Uses its
        # own fresh session: `db` may be mid-rollback from an exception
        # handler above.
        try:
            from app.database import SessionLocal as _SessionLocalFinal
            _db_release = _SessionLocalFinal()
            try:
                release_worker_attempt(_db_release, item_id, user_id, attempt_token)
            finally:
                _db_release.close()
        except Exception:
            logger.exception("[ocr] failed to release worker attempt for %s", item_id)
        db.close()


# ── POST /library/{item_id}/ocr ───────────────────────────────────────────────
@router.post("/{item_id}/ocr", response_model=LibraryItemResponse)
def start_ocr(
    item_id: str,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Opt in to reading a scanned PDF with OCR. Free to the user, and slow —
    the client says so and lets them carry on using the app meanwhile."""
    from app.services import ocr_service

    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == current_user.id,
        LibraryItem.deletion_state.is_(None),  # a tombstoned item is gone
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")
    if not is_source_unlocked(current_user, item):
        # OCR is real paid work (a scanned-book OCR call, then embedding) —
        # every source-bearing paid-work route must reject a locked source
        # before it begins, exactly like POST /bites/session and Connect.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now. Upgrade to read it with OCR.",
            },
        )
    if not ocr_service.is_available():
        raise HTTPException(status_code=503, detail={
            "code": "ocr_unavailable",
            "message": "Nibbler can't read scanned books just now. Please try again later.",
        })
    if not item.file_url:
        raise HTTPException(status_code=409, detail={
            "code": "no_original",
            "message": "The original file isn't stored, so it can't be re-read.",
        })

    # Task 2 final consolidated backend pass (Verified Blocker 1): capacity
    # reservation, THEN atomic worker-attempt admission under a row lock —
    # both happen synchronously, HERE, before any background task is ever
    # enqueued. Two simultaneous OCR requests for the same item race this
    # exact admission; at most one can win, so at most one worker is ever
    # enqueued (the previous `if item.ocr_status == "running": return` check
    # was a plain, unlocked read-then-write and did not actually prevent
    # this race).
    if not reserve_free_capacity(db, item, current_user.id):
        db.refresh(item)
        item.ocr_status = "failed"
        db.commit()
        db.refresh(item)
        return _to_response(item, current_user)

    attempt_id = admit_worker_attempt(db, item_id, current_user.id)
    if attempt_id is None:
        # Either a live attempt already owns this item (a concurrent OCR
        # request, or an ingestion worker still in flight for it) or the
        # item is already fully processed (a replay after success) — never
        # enqueue a second worker, never touch anyone else's capacity
        # reservation, never return the existing owner's attempt id to
        # this caller.
        db.refresh(item)
        return _to_response(item, current_user)

    item.ocr_status = "running"
    item.ocr_pages_done = 0
    item.processing_error = None
    db.commit()
    db.refresh(item)
    background_tasks.add_task(_run_ocr, item.id, current_user.id, attempt_id)
    return _to_response(item, current_user)

# ── DELETE /library/{item_id} ──────────────────────────────────────────────────


def _delete_item_images(item: LibraryItem, user_id: str) -> bool:
    """Delete this book's extracted figures from S3. True when all succeeded.

    Keys come from the stored rows and are checked against the owner-scoped
    prefix before any delete is issued. That check is the reason a compromised
    or corrupted row cannot turn this into a way to delete somebody else's
    objects: the prefix is derived from the authenticated user, not from data.

    Only the SOURCE images are removed. Sessions that referenced them are gone
    with the book; sessions of other books are untouched, because every key is
    scoped to this item.
    """
    images = item.images or []
    if not isinstance(images, list) or not images:
        return True
    prefix = "book-images/%s/%s/" % (user_id, item.id)
    s3 = S3Service()
    ok = True
    for img in images:
        key = (img or {}).get("key") if isinstance(img, dict) else None
        if not key:
            continue
        if not str(key).startswith(prefix):
            logger.error("Refusing to delete out-of-scope image key for item %s", item.id)
            ok = False
            continue
        # Each delete is isolated: one object that raises must not abandon the
        # rest, or a single transient failure leaves the remainder orphaned
        # forever with no record that they exist.
        try:
            if not s3.delete_file(key):
                ok = False
        except Exception as e:  # noqa: BLE001
            logger.error("Image delete raised for %s: %s", key, e)
            ok = False
    return ok


@router.get("/{item_id}/images/{candidate_id}")
def get_book_image(
    item_id: str,
    candidate_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Task 2 closeout (Verified Blocker 10): entitlement-revalidated
    BYTE PROXY, for its owner — replaces the prior 1-hour presigned-URL
    capability, which kept working for the rest of its hour even after a
    downgrade mid-flight (a real, reusable, revocation-proof capability
    handed to the client). Every single request now re-verifies Firebase
    identity (via `get_current_user`), item ownership, that the item is
    not tombstoned, and that the source is CURRENTLY unlocked — fetches
    the private S3 object server-side — and streams the bytes back.
    There is no capability outstanding between requests to revoke:
    downgrading mid-session makes the very next request fail immediately.

    Still NOT a 307 redirect to S3, for the same reason as before: a
    redirect would have the client follow a cross-host hop while still
    holding its `Authorization: Bearer <firebase id token>` header, and
    iOS's URLSession forwards headers across redirects by default — that
    would hand a user's Firebase token to Amazon. This endpoint fetches
    the object itself and returns the bytes directly, so no S3 URL of any
    kind — presigned or otherwise — is ever exposed to the client.

    `Cache-Control: no-store` on every response: a locally cached copy
    surviving a downgrade would be the same revocation gap through a
    different door.

    What a card persists is the API PATH — unchanged. Ownership is
    established by the QUERY, not by comparing ids: the lookup is scoped
    to this user AND this book, so an id belonging to another account is
    simply not found. Scoping by BOOK as well as owner matters because
    candidate ids were once derived from the image checksum alone.

    Mobile contract change (documented, not implemented here — mobile is
    a separate assignment): this endpoint used to return JSON
    (`{"url", "expires_in", "mime", "alt", "w", "h"}`); it now returns the
    raw image bytes directly, with `Content-Type` set to the image's real
    MIME type. `alt`/`w`/`h` are no longer returned by this call — the
    client must source that metadata from the card payload it already
    received at session-generation time (see app/services/image_select.py,
    which already attaches image identity/metadata to the card), not by
    re-deriving it from this byte-fetching endpoint.
    """
    if not candidate_id or not candidate_id.startswith("img_") or len(candidate_id) > 64:
        raise HTTPException(status_code=404, detail="Image not found.")

    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == current_user.id,
        LibraryItem.deletion_state.is_(None),  # a tombstoned item is gone
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Image not found.")
    if not is_source_unlocked(current_user, item):
        # Task 2 remediation: a book's extracted figures are source-derived
        # content exactly like its sessions/chat — a direct GET here must
        # not bypass the same lock every other source-bearing route enforces.
        raise HTTPException(
            status_code=403,
            detail={
                "code": "source_locked",
                "message": "This source is Premium-only right now. Upgrade to see its images again.",
            },
        )

    for img in (item.images or []):
        if not isinstance(img, dict) or img.get("id") != candidate_id:
            continue
        key = img.get("key") or ""
        # The stored key must still sit under this owner's and book's prefix.
        # A row that fails this was tampered with or written by a bug; either
        # way it is not something to hand to S3.
        if not key.startswith("book-images/%s/%s/" % (current_user.id, item.id)):
            logger.error("Image row %s has an out-of-scope key", candidate_id)
            raise HTTPException(status_code=404, detail="Image not found.")
        try:
            data = S3Service().download_file(key)
        except Exception as e:
            # Never expose the key, the bucket, or any provider detail —
            # a bounded, generic failure only (S3Service's client itself
            # carries a bounded connect/read timeout — see s3_service.py).
            logger.warning("Image fetch failed for %s: %s", candidate_id, type(e).__name__)
            raise HTTPException(status_code=502, detail="Image unavailable right now.")
        return Response(
            content=data,
            media_type=img.get("mime") or "image/png",
            headers={"Cache-Control": "no-store, private"},
        )

    raise HTTPException(status_code=404, detail="Image not found.")


def _finish_item_deletion_cleanup(db, item, user_id: str) -> bool:
    """PHASE 2 of item deletion (Task 2, 3rd-audit remediation #9) —
    factored out (Task 2 closeout, Verified Blocker 6) so the EXACT same
    logic backs both a user re-tapping DELETE and the autonomous
    maintenance-scheduler retry (`entitlement_service.retry_item_
    deletions`), never two independently-maintained copies that could
    drift.

    `item` must already be locked (`with_for_update()`) by the caller in
    the SAME transaction — this function only mutates/commits, it does
    not itself acquire the lock, so both callers keep full control of
    their own lock-order/scope.

    Attempts vectors/source-file/images cleanup; hard-deletes the row on
    full success, or records a durable, retryable 'failed' state with
    per-artifact detail otherwise. Returns whether cleanup fully
    succeeded (and the row was hard-deleted)."""
    item_id = item.id
    embedding_svc = EmbeddingService()
    vectors_cleared = embedding_svc.delete_item_vectors(item_id, user_id=user_id)

    file_cleared = True
    if item.file_url:
        file_cleared = S3Service().delete_file(item.file_url)

    # Extracted figures are separate S3 objects from the source file, so they
    # survive deleting the book unless deleted explicitly. Scoped to this
    # owner and this book: the keys come from the stored rows, never from user
    # input, so no path here can address another user's objects.
    images_cleared = _delete_item_images(item, user_id)

    if not (vectors_cleared and file_cleared and images_cleared):
        # Durable, retryable — never just a log line after the row is gone,
        # because the row is deliberately NOT gone yet.
        item.deletion_state = "failed"
        item.deletion_detail = {
            **(item.deletion_detail or {}),
            "last_attempt_at": datetime.utcnow().isoformat(),
            "vectors_cleared": vectors_cleared,
            "file_cleared": file_cleared,
            "images_cleared": images_cleared,
        }
        db.commit()
        logger.error(
            "Item deletion cleanup incomplete for item %s (user %s): pinecone_ok=%s "
            "s3_ok=%s images_ok=%s. Tombstoned for retry.",
            item_id, user_id, vectors_cleared, file_cleared, images_cleared,
        )
        return False

    db.delete(item)
    db.commit()
    return True


@router.delete("/{item_id}")
def delete_library_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Task 2 remediation (deletion vs. reservation/worker races, then a
    3rd-audit remediation making it a DURABLE two-phase state machine):

    Deletion is transactionally safe with respect to any concurrent
    reserve/reap/release/finalize call for the SAME item, because every one
    of those (see entitlement_service.py) locks the user row THEN the item
    row, in that order — the exact order this function also uses. Whichever
    transaction gets there first wins; the other blocks, then either finds
    the item already gone (a locked query against a hard-deleted row simply
    returns nothing — no special-case needed) or finds it already
    finalized/released and proceeds normally.

    Deleting a PENDING reservation must not strand `reserved_sources_count`
    (the bug Hermes's second audit reproduced as `PG_DELETE_PENDING`): the
    slot is released in the SAME transaction as the delete, so a worker that
    later loses the lock race (its own `finalize_successful_processing`/
    `release_reservation` call simply finds the item gone and no-ops) can
    never double-decrement it either.

    PHASE 1 (first call for a given item): lock user+item, release a
    stranded pending reservation if any, delete every DERIVED row (bites/
    notes/highlights/chat/completions — none of which has a real FK to
    library_items, so nothing cascades on its own), and mark the item
    `deletion_state='pending'` — all in ONE small, fast, durable commit.
    From that instant the item is invisible everywhere (list_library and
    every item-scoped route filter out `deletion_state IS NOT NULL`), so
    the "this source and its content are gone" promise is true immediately,
    even before the slower external cleanup below has run.

    PHASE 2 (same call, continuing — or a LATER retry, since a cleanup
    failure leaves the row TOMBSTONED rather than hard-deleted): attempt
    Pinecone/S3/image cleanup using `item.file_url`/`item.images`, which are
    still on the row precisely because it hasn't been hard-deleted yet —
    that's the "durable identity to retry" this remediation requires,
    without needing a separate snapshot that could itself go stale. Only
    once ALL of it succeeds does the row get hard-deleted for real. A
    failure sets `deletion_state='failed'` (still durable, still retryable)
    instead of disappearing into a log line after the row is already gone —
    the previous behavior. Retrying is just calling DELETE again: it finds
    the tombstoned row, skips the (already-empty) derived-row deletion, and
    re-attempts cleanup — each of vectors/S3-file/images deletion is itself
    idempotent, so redoing a partially-successful cleanup is safe.
    """
    user = (
        db.query(User).filter(User.id == current_user.id)
        .populate_existing().with_for_update().first()
    )
    # Deliberately NOT filtering out an already-tombstoned row here — a
    # retry needs to find exactly that row to finish the job.
    item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item_id, LibraryItem.user_id == current_user.id)
        .populate_existing()
        .with_for_update()
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")

    retrying = item.deletion_state is not None

    if not retrying:
        if item.entitlement_status == "pending" and user is not None:
            db.query(User).filter(User.id == current_user.id).update(
                {"reserved_sources_count": User.reserved_sources_count - 1},
                synchronize_session="fetch",
            )

        # Everything derived from this book goes with it, in the same
        # transaction as accepting the deletion — the app's own confirmation
        # copy promises the book goes "along with its stored content", and
        # this is what makes that true from PHASE 1, not only after the
        # slower external cleanup below eventually succeeds. saved_bites has
        # a real FK to daily_bites with ON DELETE CASCADE, but relying on
        # that would make this depend on engine settings rather than on this
        # code — deleted explicitly so it is true everywhere and provable.
        bite_ids = [
            r[0] for r in db.query(DailyBite.id).filter(
                DailyBite.user_id == current_user.id,
                DailyBite.library_item_id == item_id,
            ).all()
        ]
        removed = {
            "saved_bites": (
                db.query(SavedBite).filter(
                    SavedBite.user_id == current_user.id,
                    SavedBite.bite_id.in_(bite_ids),
                ).delete(synchronize_session=False) if bite_ids else 0
            ),
            "daily_bites": db.query(DailyBite).filter(
                DailyBite.user_id == current_user.id,
                DailyBite.library_item_id == item_id,
            ).delete(synchronize_session=False),
            "notes": db.query(Note).filter(
                Note.user_id == current_user.id, Note.book_id == item_id,
            ).delete(synchronize_session=False),
            "highlights": db.query(Highlight).filter(
                Highlight.user_id == current_user.id, Highlight.book_id == item_id,
            ).delete(synchronize_session=False),
            "chat_messages": db.query(ChatMessage).filter(
                ChatMessage.user_id == current_user.id, ChatMessage.book_id == item_id,
            ).delete(synchronize_session=False),
            "completions": db.query(Completion).filter(
                Completion.user_id == current_user.id, Completion.book_id == item_id,
            ).delete(synchronize_session=False),
        }
        item.deletion_state = "pending"
        item.deletion_detail = {
            "accepted_at": datetime.utcnow().isoformat(),
            "removed": removed,
        }
        db.commit()
    else:
        # Retry: derived rows are already gone from PHASE 1; report the
        # counts it recorded rather than re-deriving (there's nothing left
        # to count a second time).
        removed = (item.deletion_detail or {}).get("removed", {})

    # Task 2 closeout (Verified Blocker 6): PHASE 2's cleanup is now a
    # shared function — `_finish_item_deletion_cleanup` — so the exact
    # same logic backs BOTH a user re-tapping delete AND the autonomous
    # maintenance-scheduler retry (`entitlement_service.retry_item_
    # deletions`), never two independently-maintained copies.
    fully_cleared = _finish_item_deletion_cleanup(db, item, current_user.id)

    if not fully_cleared:
        return {
            "message": (
                "This source is gone from your Library. Finishing cleanup in the "
                "background — nothing left to do on your end."
            ),
            "removed": removed,
            "external_cleanup_complete": False,
            "deletion_state": "failed",
        }

    return {
        "message": "Item deleted successfully",
        "removed": removed,
        "external_cleanup_complete": True,
        "deletion_state": None,
    }


# ── Background tasks ───────────────────────────────────────────────────────────

# Shown on the library row when Voyage rejects the embedding batches. Before
# July 2026 this failure was silently swallowed into random mock vectors, which
# poisoned Pinecone and made the Connect goal-match read ~4% forever. Failing
# loudly is the correct behavior.
EMBEDDING_DOWN_MESSAGE = (
    "Nibbler couldn't finish reading this one — the reading service is briefly "
    "unavailable. Delete it and upload again in a few minutes."
)


def _extract_book_images(db, item, file_bytes: bytes, user_id: str, attempt_token: str) -> int:
    """Extract this book's figures onto `item.images`. Never raises.

    Runs after text extraction and indexing have already succeeded, so the only
    thing at risk is the pictures themselves. Everything is caught: Pillow
    missing, a malformed PDF stream, S3 down, an EPUB with a broken OPF. All of
    those end with a normal text-only book, which is the expected state for
    most uploads anyway.

    Existing library items are NOT reprocessed. A book uploaded before this
    feature has `images = None` and keeps producing text-only sessions, which
    is correct — re-reading every stored file to hunt for figures would be a
    large, silent, retroactive S3 bill.

    Task 2 closeout (Verified Blocker 3): `attempt_token` is threaded all
    the way through — into the S3 key (see `image_extract.image_key`, so a
    stale attempt's own uploaded objects can never collide with or be
    deleted alongside a newer attempt's), checked BEFORE the (CPU- and
    S3-bound) extraction pass even starts, and re-verified ATOMICALLY,
    under a row lock, in the SAME transaction that persists `item.images`
    — never a plain existence probe followed by a separate write. Runs
    strictly inside the caller's worker-attempt lifetime: the caller
    releases the attempt only in its own `finally` block, AFTER this
    function returns."""
    from app.services.image_extract import extract_and_store, pdf_page_texts

    item_id = item.id

    # Cheap pre-check, before any extraction/upload work: a stale or
    # tombstoned attempt skips the whole pass rather than discovering the
    # loss only after paying for it.
    pre = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
    if not pre or pre.deletion_state is not None or pre.last_processing_attempt_id != attempt_token:
        logger.info("[images] %s: skipping extraction — attempt %s no longer owns this item",
                     item_id, attempt_token)
        return 0

    try:
        is_epub = (item.type or "").lower() == "epub"
        page_texts = None
        if not is_epub:
            try:
                page_texts = pdf_page_texts(file_bytes)
            except Exception:
                # Page text only sharpens relevance matching; without it the
                # candidates are still usable, just less well described.
                page_texts = None

        images = extract_and_store(
            file_bytes=file_bytes,
            filename=("x.epub" if is_epub else "x.pdf"),
            item_id=item_id,
            user_id=user_id,
            page_texts=page_texts,
            attempt_token=attempt_token,
        )
        if not images:
            return 0

        # Ownership re-verified AND item.images persisted atomically, in
        # ONE locked transaction (_atomic_ownership_write also refuses a
        # tombstoned item — see its own docstring) — not a separate
        # existence probe followed by a plain write, which left a real
        # window open the same way every Blocker 2 write point did before
        # this pass. A RAISED exception (e.g. the commit itself failing)
        # is treated identically to an ordinary `False` return — both
        # mean "not durably recorded", and both must run the SAME
        # durable per-image cleanup below rather than let the raise
        # escape to the generic catch-all further down, which has no
        # per-image cleanup logic of its own.
        try:
            applied = _atomic_ownership_write(
                db, item_id, user_id, attempt_token,
                lambda locked: setattr(locked, "images", images),
            )
        except Exception as e:
            logger.error("[images] could not persist rows for %s (%s)", item_id, e)
            try:
                db.rollback()
            except Exception:
                pass
            applied = False
        if not applied:
            # The objects are already in S3, and this attempt has lost
            # (or never had) the right to record them. Durable, per-image
            # compensating cleanup — never an unconditional best-effort
            # delete with no durable trace on failure — because the
            # objects are invisible to book deletion/account erasure
            # until either they're gone or a retryable record exists.
            logger.error(
                "[images] attempt %s lost ownership of %s before its images "
                "could be persisted — cleaning up %d uploaded object(s)",
                attempt_token, item_id, len(images),
            )
            for img in images:
                key = (img or {}).get("key")
                if not key:
                    continue
                _cleanup_one_image_after_ownership_loss(
                    item_id, user_id, attempt_token, key,
                    reason="ownership lost before images could be persisted",
                )
            return 0
        return len(images)
    except Exception as e:
        logger.warning("[images] extraction skipped for %s: %s", item_id, e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _record_processing_error(item_id: str, message: str, attempt_token: str) -> bool:
    """Attempt-scoped processing-error write (Task 2 lifecycle remediation,
    Follow-up 2A — `attempt_token` is now REQUIRED and validated against
    the item's current `last_processing_attempt_id`, never optional and
    never trusted blind). A stale attempt calling this after being
    superseded is a safe, silent no-op — never a mutation of a newer
    attempt's row. Returns whether the write actually happened, so a
    caller that cares can tell the two apart."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        # Task 2 final consolidated backend pass (Verified Blocker 2): row
        # lock + re-verify + write in ONE transaction, not a bare SELECT
        # followed by a separate UPDATE — the earlier check alone left a
        # real window for a concurrent admit_worker_attempt to supersede
        # ownership between the read and this write.
        item = (
            db.query(LibraryItem)
            .filter(LibraryItem.id == item_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not item or item.last_processing_attempt_id != attempt_token:
            return False
        item.processing_error = message[:250]
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()


def _cleanup_ledger_upsert_pending(
    item_id: str, user_id: str, attempt_token: str, artifact_kind: str,
    artifact_key: str, reason: str,
) -> str:
    """Durable, attempt-scoped cleanup-needed record (Task 2 lifecycle
    remediation, Follow-up 2A), written into the SEPARATE `cleanup_tasks`
    ledger (app/models/library.py's `CleanupTask`) BEFORE cleanup is
    attempted — a crash/outage mid-cleanup still leaves a retryable trace.
    A record here is completely independent of whoever currently owns the
    `library_items` row: a stale attempt's own cleanup identity is never
    at the mercy of a newer attempt's ownership, and one attempt can hold
    INDEPENDENT vector and S3 records at once. Idempotent — a retry
    updates the SAME row (by item/attempt/kind) rather than piling up
    duplicates.

    Also mirrors onto `LibraryItem.cleanup_state`/`cleanup_detail` — but
    ONLY while `attempt_token` still matches the row's current
    `last_processing_attempt_id` — for the still-current-owner case this
    is a strict superset of the ledger (same information, visible directly
    on the row without a join); for a SUPERSEDED attempt the row mirror is
    correctly skipped (a stale attempt has nothing it can safely claim on
    a row a newer attempt now owns) and the ledger record above is the
    ONLY durable trace.

    Returns one of `"inserted"` / `"existing"` / `"failed"` (Task 2
    consolidated backend pass — this used to return nothing at all, so a
    caller had no way to tell "my write durably landed" from "it silently
    lost a race" from "persistence itself failed"). Race-safe under real
    concurrent PostgreSQL callers for the identical (item, attempt, kind)
    identity: two sessions can both observe "no existing row" under
    `SELECT ... FOR UPDATE` only if their transactions don't overlap on
    the SAME row — the loser's `INSERT` then raises `IntegrityError`
    against the table's real unique constraint, caught below and
    reclassified as `"existing"` (a real, accounted-for identity now
    exists — not a persistence failure) rather than left as an ordinary,
    unclassified exception."""
    if not attempt_token:
        return "failed"
    from app.database import SessionLocal
    from app.models.library import CleanupTask
    from sqlalchemy.exc import IntegrityError
    # Task 2 closeout (Verified Blocker 4): NEVER stored as NULL — the
    # unique constraint (item_id, attempt_token, artifact_kind,
    # artifact_key) only actually prevents duplicates if every row has a
    # real, comparable key. "" is the normalized "no natural key" value
    # for kinds like 'vectors'; a real S3 key is used as-is, so multiple
    # images for the SAME attempt each get their own independent row.
    norm_key = artifact_key or ""
    db = SessionLocal()
    try:
        existing = (
            db.query(CleanupTask)
            .filter(
                CleanupTask.item_id == item_id,
                CleanupTask.attempt_token == attempt_token,
                CleanupTask.artifact_kind == artifact_kind,
                CleanupTask.artifact_key == norm_key,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        now = datetime.utcnow()
        if existing:
            if existing.user_id != user_id:
                # A genuine identity conflict — never silently rewrite an
                # existing durable record's owner. Should be unreachable
                # in practice (attempt_token is minted per-user), but
                # refuse rather than corrupt a real record.
                logger.error(
                    "cleanup ledger identity conflict for item=%s attempt=%s "
                    "kind=%s key=%r: existing user %s != caller %s",
                    item_id, attempt_token, artifact_kind, norm_key,
                    existing.user_id, user_id,
                )
                db.rollback()
                return "failed"
            existing.cleanup_state = "pending"
            existing.reason = (reason or "")[:250]
            existing.updated_at = now
            outcome = "existing"
        else:
            db.add(CleanupTask(
                id=str(uuid.uuid4()), item_id=item_id, user_id=user_id,
                attempt_token=attempt_token, artifact_kind=artifact_kind,
                artifact_key=norm_key, cleanup_state="pending",
                reason=(reason or "")[:250], retry_count=0,
            ))
            outcome = "inserted"
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if item and item.last_processing_attempt_id == attempt_token:
            item.cleanup_state = "pending"
            item.cleanup_detail = {
                "attempt_token": attempt_token,
                "s3_key": norm_key if artifact_kind in ("s3", "s3_image") and norm_key else None,
                "reason": (reason or "")[:250],
            }
        db.commit()
        return outcome
    except IntegrityError:
        db.rollback()
        # A genuinely concurrent caller for the IDENTICAL identity won the
        # race between our own SELECT and INSERT. Task 2 closeout
        # (Verified Blocker 4): an IntegrityError alone is not proof of
        # WHAT landed — re-read and verify the COMPLETE exact identity
        # (including user_id) before trusting it as "existing"; if it
        # cannot be verified, this is a genuine persistence failure, not
        # a benign race.
        verify_db = SessionLocal()
        try:
            row = (
                verify_db.query(CleanupTask)
                .filter(
                    CleanupTask.item_id == item_id,
                    CleanupTask.attempt_token == attempt_token,
                    CleanupTask.artifact_kind == artifact_kind,
                    CleanupTask.artifact_key == norm_key,
                    CleanupTask.user_id == user_id,
                )
                .first()
            )
            return "existing" if row is not None else "failed"
        finally:
            verify_db.close()
    except Exception:
        db.rollback()
        logger.exception("failed to persist cleanup-pending ledger record for %s/%s", item_id, artifact_kind)
        return "failed"
    finally:
        db.close()


def _cleanup_ledger_claim_direct(
    item_id: str, attempt_token: str, artifact_kind: str, artifact_key: str = None,
) -> "str | None":
    """Task 2 closeout (Verified Blocker 4): atomically claim an existing
    pending/failed ledger row for a DIRECT (in-process, right-after-
    failure) cleanup attempt, using the SAME claim protocol
    `retry_cleanup_tasks` uses for the autonomous scheduler runner — so a
    direct compensation call and a scheduler retry can never both call
    the provider for the identical artifact at the same time.

    Returns a fresh claim id on success (pass it to `_cleanup_ledger_
    resolve` as `expected_claimed_by`), or `None` when the row is
    missing/already resolved, or already claimed by a currently-live
    runner — in which case that OTHER claimant owns finishing this exact
    cleanup, and this caller must NOT also call the provider."""
    from app.database import SessionLocal
    from app.models.library import CleanupTask
    from datetime import timedelta
    norm_key = artifact_key or ""
    db = SessionLocal()
    try:
        row = (
            db.query(CleanupTask)
            .filter(
                CleanupTask.item_id == item_id,
                CleanupTask.attempt_token == attempt_token,
                CleanupTask.artifact_kind == artifact_kind,
                CleanupTask.artifact_key == norm_key,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not row or row.cleanup_state not in ("pending", "failed"):
            return None
        now = datetime.utcnow()
        if row.claimed_until is not None and row.claimed_until >= now:
            return None  # a currently-live runner already holds this exact row
        claim_id = f"direct-{uuid.uuid4()}"
        row.claimed_by = claim_id
        row.claimed_until = now + timedelta(minutes=2)
        db.commit()
        return claim_id
    except Exception:
        db.rollback()
        logger.exception(
            "failed to claim cleanup ledger row for direct compensation (%s/%s/%s)",
            item_id, artifact_kind, norm_key,
        )
        return None
    finally:
        db.close()


def _cleanup_ledger_release_claim(
    item_id: str, attempt_token: str, artifact_kind: str, artifact_key: str, claim_id: str,
) -> None:
    """Task 2 closeout (Verified Blocker 4): release ONLY this exact claim
    — mirrors the release step `retry_cleanup_tasks` already performs for
    the scheduler runner. Without this, a direct claim would sit active
    for its full TTL after resolution, blocking a legitimate immediate
    retry (e.g. the SAME code path called again right after a failure)
    from ever claiming the row again until the lease happens to expire.
    Guarded by `claimed_by == claim_id`: if a newer claimant has since
    taken the row, this must not release THEIR lease early."""
    from app.database import SessionLocal
    from app.models.library import CleanupTask
    norm_key = artifact_key or ""
    db = SessionLocal()
    try:
        row = (
            db.query(CleanupTask)
            .filter(
                CleanupTask.item_id == item_id,
                CleanupTask.attempt_token == attempt_token,
                CleanupTask.artifact_kind == artifact_kind,
                CleanupTask.artifact_key == norm_key,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if row and row.claimed_by == claim_id:
            row.claimed_by = None
            row.claimed_until = None
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("failed to release direct cleanup claim for %s/%s/%s", item_id, artifact_kind, norm_key)
    finally:
        db.close()


def _cleanup_ledger_resolve(
    item_id: str, attempt_token: str, artifact_kind: str, ok: bool, error_detail: str = None,
    expected_claimed_by: str = None, artifact_key: str = None,
) -> bool:
    """Resolve (success) or fail the ledger record this attempt/kind/key
    identifies. Task 2 lifecycle remediation, Follow-up 2A: an ORDINARY
    `False` return from the provider is exactly as durable a failure as a
    raised exception — both land here with `ok=False` — never silently
    treated as success the way the old `try/except Exception` alone did.

    `artifact_key` (Task 2 closeout, Verified Blocker 4): identifies the
    EXACT row now that one attempt can hold several independent 's3_image'
    records at once — omitted (None, normalized to "") for 'vectors'/'s3',
    which have at most one row per attempt.

    `expected_claimed_by` (Task 2 consolidated backend pass, correction):
    when given (the autonomous `retry_cleanup_tasks` runner, or a direct
    caller that claimed via `_cleanup_ledger_claim_direct`), this resolve
    is a no-op unless the row's CURRENT `claimed_by` still matches
    exactly — a LATE-resolving runner whose claim has since expired and
    been reclaimed by a newer runner must never overwrite that newer
    runner's own, already-authoritative result. A caller that never
    claims anything passes `None`, skipping this check entirely.

    Mirrors onto `LibraryItem.cleanup_state`/`cleanup_detail` under the
    same still-current-owner condition `_cleanup_ledger_upsert_pending`
    uses — cleared on success, left 'failed' (with identity retained) on
    failure — but only ever touches the row for the attempt CURRENTLY
    named in `cleanup_detail`, so a stale attempt's late resolution can
    never clear or overwrite a newer attempt's own marker.

    On a successful ('s3') resolution, also clears `LibraryItem.file_url`/
    `archive_status` when they still literally equal the exact key that
    was just deleted — mirroring the SAME identity check the DIRECT
    compensation path (`_cleanup_archive_after_abandoned_processing`)
    already uses. Without this, a successful AUTONOMOUS retry (the
    scheduled `retry_cleanup_tasks` runner, which calls this — not that
    direct path — to resolve) deleted the real S3 object but left the row
    still claiming an archived file at that now-deleted key. Gated on the
    literal key match (not on `cleanup_detail`/attempt ownership) so it
    can never clear a NEWER attempt's own, different archive key.

    Returns whether this call's state transition actually COMMITTED
    (Task 2 closeout, Verified Blocker 4) — `False` for a missing row or
    a claim mismatch, so a caller (in particular the autonomous runner)
    can tell "I resolved this" from "someone else already did, or it was
    never here" and count outcomes accordingly."""
    if not attempt_token:
        return False
    from app.database import SessionLocal
    from app.models.library import CleanupTask
    norm_key = artifact_key or ""
    db = SessionLocal()
    try:
        existing = (
            db.query(CleanupTask)
            .filter(
                CleanupTask.item_id == item_id,
                CleanupTask.attempt_token == attempt_token,
                CleanupTask.artifact_kind == artifact_kind,
                CleanupTask.artifact_key == norm_key,
            )
            .populate_existing()
            .with_for_update()
            .first()
        )
        if not existing:
            return False
        if expected_claimed_by is not None and existing.claimed_by != expected_claimed_by:
            # A newer claim holder has since taken this exact row — a
            # late/stale runner must not resolve or overwrite it.
            return False
        if ok:
            existing.cleanup_state = "resolved"
            existing.reason = None
        else:
            existing.cleanup_state = "failed"
            if error_detail:
                existing.reason = error_detail[:250]
            existing.retry_count = (existing.retry_count or 0) + 1

        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if (
            item and isinstance(item.cleanup_detail, dict)
            and item.cleanup_detail.get("attempt_token") == attempt_token
        ):
            if ok:
                item.cleanup_state = None
                item.cleanup_detail = None
            else:
                item.cleanup_state = "failed"
        if ok and artifact_kind == "s3" and norm_key and item and item.file_url == norm_key:
            item.file_url = None
            item.archive_status = None
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("failed to resolve cleanup ledger record for %s/%s", item_id, artifact_kind)
        return False
    finally:
        db.close()


def _cleanup_vectors_after_abandoned_processing(
    item_id: str, user_id: str, attempt_token: str = None, reason: str = None,
) -> bool:
    """Compensating cleanup (Task 2 remediation #5, tightened to be
    attempt-scoped by the 3rd audit): every ingestion path writes vectors to
    Pinecone BEFORE the database conversion that makes them "count" — a
    finalize rejection (capacity ran out between reserve and finalize, a
    superseded lease, or the item was deleted mid-flight) or any exception
    AFTER the paid indexing call can leave those vectors orphaned with
    nothing in Postgres pointing at them.

    `attempt_token`, when given, scopes the Pinecone delete to vectors THIS
    attempt wrote (see EmbeddingService.index_text/delete_item_vectors) —
    required because vector ids are deterministic and get overwritten in
    place by a retry: an unscoped delete run by a STALE attempt after a
    newer attempt has already written could otherwise delete the newer
    attempt's live vectors. Omitting it (legacy/defensive callers) falls
    back to the old item-wide delete.

    Unconditional and idempotent: called from every terminal failure/
    rejection path regardless of whether indexing actually reached Pinecone
    — deleting vectors that were never written (or that no longer match the
    scoping filter) is a safe no-op. Scoped to this exact user+item, so it
    can never touch another account's data. Returns whether it actually
    succeeded — Task 2 lifecycle remediation: an ordinary `False` return is
    now durable failure, not silently treated as success.

    Task 2 closeout (Verified Blocker 4): the durable ledger record must
    persist BEFORE any irreversible provider call — if persistence itself
    fails, this returns False WITHOUT ever calling Pinecone, rather than
    deleting something no durable record will ever point back to. Also
    atomically CLAIMS the row before calling the provider (the same claim
    protocol `retry_cleanup_tasks` uses), so a concurrent scheduler retry
    for the identical artifact cannot also call the provider at the same
    time — mutual exclusion between the direct and scheduled paths."""
    outcome = _cleanup_ledger_upsert_pending(item_id, user_id, attempt_token, "vectors", None, reason or "vector cleanup")
    if outcome == "failed":
        logger.error("cleanup ledger persistence failed for item %s (vectors) — refusing to call the provider", item_id)
        return False
    claim_id = _cleanup_ledger_claim_direct(item_id, attempt_token, "vectors", None)
    if claim_id is None:
        # Already claimed by a currently-live runner (a concurrent
        # scheduler retry, most likely) — that claimant owns finishing
        # this exact cleanup; calling the provider here too would be a
        # duplicate action against the same artifact.
        logger.info("vector cleanup for item %s already claimed by another runner — skipping duplicate provider call", item_id)
        return True
    ok = False
    error_detail = None
    try:
        result = EmbeddingService().delete_item_vectors(item_id, user_id=user_id, attempt_token=attempt_token)
        ok = bool(result)
        if not ok:
            error_detail = "delete_item_vectors returned False"
    except Exception as e:
        ok = False
        error_detail = str(e)[:250]
        logger.exception("compensating vector cleanup failed for item %s", item_id)
    _cleanup_ledger_resolve(item_id, attempt_token, "vectors", ok, error_detail, expected_claimed_by=claim_id)
    _cleanup_ledger_release_claim(item_id, attempt_token, "vectors", None, claim_id)
    return ok


def _cleanup_archive_after_abandoned_processing(
    item_id: str, attempt_token: str, s3_key: str, reason: str = None, user_id: str = None,
) -> bool:
    """Delete an attempt-owned S3 archive file after that attempt
    terminally failed. Task 2 lifecycle remediation, Follow-up 2A: PDF/
    EPUB/etc. archive keys are now ATTEMPT-SCOPED (see
    process_pdf_embeddings and friends — `{user_id}/{item_id}/
    {attempt_token}.<ext>`, never the old fixed `{user_id}/{item_id}.<ext>`)
    rather than a single fixed per-item key, so THIS attempt's key can
    never collide with a different attempt's key at all — deleting it can
    never remove a newer attempt's live object regardless of who currently
    owns the `library_items` row, closing the check-then-delete race the
    old fixed-key design was exposed to. `item.file_url`/`archive_status`
    are cleared only when they still literally equal THIS exact key, which
    is only ever true if no newer attempt has since archived over them —
    so a stale attempt's cleanup can prove, structurally, that it never
    overwrites a newer attempt's own row fields either.

    Returns whether the delete succeeded — an ordinary `False` return from
    S3Service.delete_file is now durable failure too, not silently treated
    as success.

    `user_id` (Task 2 closeout follow-up): every real caller
    (`_compensate_failed_attempt`) already knows the owning user — passing
    it directly means this function no longer needs to look up the
    `library_items` row just to learn it. Without this, a `library_items`
    row that has ALREADY been deleted (a real, independent deletion racing
    this attempt's own compensation) made the old row-lookup-only path
    silently `return True` — claiming success — without ever attempting
    the S3 delete or writing a durable ledger record, because it had no
    other way to learn `user_id`. `item_id`/`attempt_token`/`s3_key` fully
    identify the artifact regardless of whether the row still exists, so
    once `user_id` is supplied directly there is nothing left that
    requires the row to still be there. Falls back to the row lookup only
    when `user_id` is omitted (legacy/defensive callers), preserving the
    old row-required behavior for that narrower case.

    Task 2 closeout (Verified Blocker 4): persists the durable ledger
    record and atomically claims it BEFORE calling S3 — see the matching
    note on `_cleanup_vectors_after_abandoned_processing`."""
    if not s3_key:
        return True
    from app.database import SessionLocal
    if user_id is None:
        db = SessionLocal()
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if not item:
                return True  # no known owner and the item itself is gone — nothing left to clean up under it
            user_id = item.user_id
        finally:
            db.close()

    outcome = _cleanup_ledger_upsert_pending(item_id, user_id, attempt_token, "s3", s3_key, reason or "archive cleanup")
    if outcome == "failed":
        logger.error("cleanup ledger persistence failed for item %s (s3) — refusing to call the provider", item_id)
        return False
    claim_id = _cleanup_ledger_claim_direct(item_id, attempt_token, "s3", s3_key)
    if claim_id is None:
        logger.info("archive cleanup for item %s already claimed by another runner — skipping duplicate provider call", item_id)
        return True

    ok = False
    error_detail = None
    db2 = SessionLocal()
    try:
        try:
            result = S3Service().delete_file(s3_key)
            ok = bool(result)
            if not ok:
                error_detail = "delete_file returned False"
        except Exception as e:
            ok = False
            error_detail = str(e)[:250]
            logger.exception("compensating archive cleanup failed for item %s", item_id)
        if ok:
            item = db2.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item and item.file_url == s3_key:
                item.file_url = None
                item.archive_status = None
                db2.commit()
    finally:
        db2.close()

    _cleanup_ledger_resolve(item_id, attempt_token, "s3", ok, error_detail, expected_claimed_by=claim_id, artifact_key=s3_key)
    _cleanup_ledger_release_claim(item_id, attempt_token, "s3", s3_key, claim_id)
    return ok


def _cleanup_one_image_after_ownership_loss(
    item_id: str, user_id: str, attempt_token: str, image_key: str, reason: str = None,
) -> bool:
    """Task 2 closeout (Verified Blocker 3/4): durable, per-image compensating
    cleanup for ONE attempt-scoped image object this attempt uploaded to S3
    but was refused ownership to persist (see `_extract_book_images`) —
    artifact_kind 's3_image', keyed by the EXACT image key, so a book with
    several images gets several INDEPENDENT durable rows for the SAME
    attempt, each resolved on its own. Same persist-then-claim-then-call
    discipline as the vector/archive cleanup functions above: no provider
    call happens unless the durable ledger record persists first, and the
    row is claimed before S3 is touched so a concurrent scheduler retry for
    the identical key cannot also delete it."""
    if not image_key:
        return True
    outcome = _cleanup_ledger_upsert_pending(item_id, user_id, attempt_token, "s3_image", image_key, reason or "orphaned image cleanup")
    if outcome == "failed":
        logger.error("cleanup ledger persistence failed for item %s (s3_image %s) — refusing to call the provider", item_id, image_key)
        return False
    claim_id = _cleanup_ledger_claim_direct(item_id, attempt_token, "s3_image", image_key)
    if claim_id is None:
        logger.info("image cleanup for item %s key %s already claimed by another runner — skipping duplicate provider call", item_id, image_key)
        return True

    ok = False
    error_detail = None
    try:
        result = S3Service().delete_file(image_key)
        ok = bool(result)
        if not ok:
            error_detail = "delete_file returned False"
    except Exception as e:
        ok = False
        error_detail = str(e)[:250]
        logger.exception("compensating image cleanup failed for item %s key %s", item_id, image_key)

    _cleanup_ledger_resolve(item_id, attempt_token, "s3_image", ok, error_detail, expected_claimed_by=claim_id, artifact_key=image_key)
    _cleanup_ledger_release_claim(item_id, attempt_token, "s3_image", image_key, claim_id)
    return ok


def _compensate_failed_attempt(
    item_id: str, user_id: str, attempt_token: str = None, s3_key: str = None, reason: str = None,
) -> bool:
    """Single entry point every processing pipeline's terminal-failure path
    calls: attempt-scoped vector cleanup, plus attempt-scoped archive
    cleanup when this attempt wrote one. Both halves treat an ordinary
    `False` return exactly as durably as a raised exception (Task 2
    lifecycle remediation, Follow-up 2A) and persist pending/failed/
    resolved state into the `cleanup_tasks` ledger, independent of
    whichever attempt currently owns the `library_items` row. `reason` is
    passed through as the initial ledger detail for both artifact kinds.
    Returns whether BOTH halves succeeded."""
    ok_vectors = _cleanup_vectors_after_abandoned_processing(item_id, user_id, attempt_token, reason=reason)
    ok_s3 = True
    if s3_key:
        ok_s3 = _cleanup_archive_after_abandoned_processing(
            item_id, attempt_token, s3_key, reason=reason, user_id=user_id)
    return ok_vectors and ok_s3


def _release_reservation_after_failure(
    item_id: str, user_id: str, message: str, lease_token: str = None,
    attempt_token: str = None, s3_key: str = None,
) -> None:
    """Paid processing failed after a reservation was already made for this
    item — return the slot to available capacity rather than stranding it
    (a no-op for an item that was never reserved, was 'premium', or no
    longer exists), AND run attempt-scoped compensating cleanup (vectors,
    plus the archived file when this attempt wrote one). Uses its own
    session/connection: the caller's `db` may already be in a rolled-back
    state from the exception this is cleaning up after.

    `lease_token` (the CAPACITY lease — `item.reservation_lease_token`,
    always None for a 'premium' item) and `attempt_token` (the durable
    ATTEMPT identity used for cleanup scoping — `reservation_lease_token or
    last_processing_attempt_id`, present for every tier) are deliberately
    two different parameters: `release_reservation`'s own `entitlement_status
    != 'pending'` check already makes it a safe no-op for 'premium' items
    regardless of what's passed, but `finalize_successful_processing`
    (called elsewhere, not here) checks a non-None lease_token BEFORE it
    checks status — passing the attempt id (never None once reserved) as
    lease_token for a 'premium' item would incorrectly fail that check.
    Callers must never conflate the two."""
    if attempt_token is None:
        attempt_token = lease_token
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if item:
            release_reservation(db, item, user_id, reason=message[:250], lease_token=lease_token)
    except Exception:
        db.rollback()
        logger.exception("release_reservation failed for item %s", item_id)
    finally:
        db.close()
    _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, s3_key=s3_key, reason=message)


def process_item_embeddings(item_id: str, user_id: str):
    """Chunk plain-text / pasted content and upsert to Pinecone."""
    from app.database import SessionLocal
    db = SessionLocal()
    lease_token = None    # capacity lease — None for a 'premium' attempt
    attempt_token = None  # immutable durable attempt identity — retained
                           # for this call's ENTIRE lifetime
    guard = None
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item or not item.content:
            return

        # Reserve BEFORE the paid embedding call — see entitlement_service
        # docstring. A rejection here means no Voyage/Pinecone call is ever
        # made and no vectors are ever created for this item.
        if not reserve_free_capacity(db, item, user_id):
            db.commit()
            return
        db.refresh(item)
        # Task 2 remediation (3rd audit, all-pipeline attempt ownership):
        # every pipeline now captures the SAME durable attempt identity OCR
        # already tracked, and threads it into indexing + finalize/release
        # so a stale worker can never mutate/finalize a newer attempt's
        # reservation, and compensating cleanup can never delete a newer
        # attempt's vectors (see EmbeddingService.index_text/delete_item_vectors).
        # `lease_token` (raw reservation_lease_token — None for 'premium')
        # is what finalize_successful_processing's capacity check needs;
        # `attempt_token` (falls back to last_processing_attempt_id) is
        # what indexing/cleanup scoping needs regardless of tier — the two
        # must NOT be conflated, or a premium item's finalize call would be
        # incorrectly rejected (see the docstring on _release_reservation_after_failure).
        lease_token = item.reservation_lease_token
        # Task 2 final consolidated backend pass (Verified Blocker 1):
        # capacity reservation and worker-attempt admission are separate
        # concerns now — admit_worker_attempt mints a FRESH attempt id for
        # THIS invocation, atomically under a row lock, and rejects
        # (without touching anyone else's capacity reservation — requirement
        # 12) if a live attempt already owns the item or it is already fully
        # processed (a replay after success).
        attempt_token = admit_worker_attempt(db, item_id, user_id)
        if attempt_token is None:
            logger.info("[process_item_embeddings] %s: worker-attempt admission rejected "
                        "(already live or already processed)", item_id)
            return

        # Full-attempt heartbeat (Task 2 lifecycle remediation, Follow-up
        # 2A): text is the pipeline with NO natural checkpoint at all
        # between reservation and the finished result — the background
        # thread is the ONLY coverage this phase gets.
        guard = _AttemptGuard(item_id, user_id, lease_token, attempt_token)
        guard.start()

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=item.content,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": item.type},
            attempt_token=attempt_token,
            on_batch=guard.checkpoint,
            check_ownership=guard.check,
        )
        guard.check()
        if not finalize_successful_processing(db, item, user_id, chunk_count, lease_token=lease_token, attempt_token=attempt_token):
            # Capacity was reserved above, so this should not normally
            # happen — but a concurrent deletion or a superseded reservation
            # can still land here, and the vectors just written above must
            # not be left orphaned.
            _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, reason="finalize rejected")
    except AttemptOwnershipLost as e:
        db.rollback()
        logger.warning("[process_item_embeddings] %s aborted — %s", item_id, e)
        _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, reason="ownership lost")
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_item_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, EMBEDDING_DOWN_MESSAGE, lease_token=lease_token, attempt_token=attempt_token,
        )
    except Exception as e:
        # Without this the row sat processed=False forever with no error —
        # the app polled endlessly with nothing to show the user.
        db.rollback()
        print(f"[process_item_embeddings] Error for item {item_id}: {e}")
        message = f"Processing failed: {str(e)[:250]}"
        _record_processing_error(item_id, message, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, message, lease_token=lease_token, attempt_token=attempt_token,
        )
    finally:
        if guard is not None:
            try:
                guard.stop()
            except GuardShutdownFailed as e:
                # Task 2 final consolidated backend pass (Verified Blocker
                # 2): a surviving guard thread is a real operational
                # problem — logged loudly, never silently swallowed — but
                # must not skip this function's own db.close() below.
                logger.error("[attempt-guard] %s", e)
        if attempt_token is not None:
            # Release the worker-attempt claim on EVERY terminal outcome —
            # idempotent (a no-op if this attempt is no longer the current
            # owner) — so a legitimate future retry can be admitted. Own
            # fresh session: `db` may be mid-rollback from a handler above.
            try:
                from app.database import SessionLocal as _SessionLocalFinal
                _db_release = _SessionLocalFinal()
                try:
                    release_worker_attempt(_db_release, item_id, user_id, attempt_token)
                finally:
                    _db_release.close()
            except Exception:
                logger.exception("failed to release worker attempt for %s", item_id)
        db.close()


def process_pdf_embeddings(item_id: str, pdf_bytes: bytes, user_id: str):
    """Extract text from the uploaded PDF bytes, chunk, and upsert to
    Pinecone. Works straight from the request payload — no S3 round-trip,
    so processing succeeds even when file archival is unavailable.

    Task 2 remediation (3rd audit, reserve-before-side-effect #3): the
    reservation now happens BEFORE the S3 archive upload — archival is real
    paid/billed storage work, and an account already at capacity must never
    pay for it. `reserve_free_capacity` is idempotent for an item that's
    already reserved, so if OCR later continues this same attempt (the "no
    text, needs OCR" branch below), it picks up the identical reservation
    and attempt token rather than reserving a second time.
    """
    from app.database import SessionLocal
    from app.services.text_extract import pdf_to_structured_text

    db = SessionLocal()
    lease_token = None    # capacity lease — None for a 'premium' attempt
    attempt_token = None  # immutable durable attempt identity — retained
                           # for this call's ENTIRE lifetime
    s3_key = None
    archived = False
    guard = None
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        # Reserve BEFORE any external side effect (S3 archive, extraction).
        # A rejection here means no S3/Voyage/Pinecone call is ever made.
        if not reserve_free_capacity(db, item, user_id):
            db.commit()
            return
        db.refresh(item)
        # See process_item_embeddings for why these are two separate values
        # (lease_token is None for a 'premium' attempt; conflating them would
        # make finalize_successful_processing incorrectly reject a premium upload).
        lease_token = item.reservation_lease_token
        # Task 2 final consolidated backend pass (Verified Blocker 1):
        # capacity reservation and worker-attempt admission are separate
        # concerns now — admit_worker_attempt mints a FRESH attempt id for
        # THIS invocation, atomically under a row lock, and rejects
        # (without touching anyone else's capacity reservation — requirement
        # 12) if a live attempt already owns the item or it is already fully
        # processed (a replay after success).
        attempt_token = admit_worker_attempt(db, item_id, user_id)
        if attempt_token is None:
            logger.info("[process_pdf_embeddings] %s: worker-attempt admission rejected "
                        "(already live or already processed)", item_id)
            return
        # Attempt-scoped archive key (Task 2 lifecycle remediation, Follow-up
        # 2A) — was a FIXED `{user_id}/{item_id}.pdf`, overwritten in place by
        # every retry, which is exactly what let a stale attempt's delayed
        # compensation delete a NEWER attempt's already-live file. Keying by
        # the immutable attempt token (opaque, not a mutable timestamp) means
        # two attempts for the same item can never collide on one object.
        s3_key = f"{user_id}/{item_id}/{attempt_token}.pdf"

        # Full-attempt heartbeat starts the instant the reservation commits —
        # covers the archive upload, extraction, and pre-first-batch
        # embedding-generation phases below, none of which has a natural
        # per-operation checkpoint of their own.
        guard = _AttemptGuard(item_id, user_id, lease_token, attempt_token)
        guard.start()

        # Best-effort archive of the original file (needs AWS keys on Railway;
        # skipped silently when unavailable — nothing downstream depends on it)
        guard.check()
        try:
            s3 = S3Service()
            uploaded_key = s3.upload_file(
                file_content=pdf_bytes,
                filename=s3_key,
                content_type="application/pdf",
            )
            # `archived` (used below to decide whether compensating cleanup
            # gets `s3_key`) must track whether the S3 OBJECT itself was
            # actually written — set the instant `upload_file` returns,
            # not after the row write that follows also succeeds. Setting
            # it only on a successful `_atomic_ownership_write` (Section O,
            # real repro) let a genuinely-uploaded object become
            # unreachable: the row write can legitimately fail with
            # `AttemptOwnershipLost` (this attempt was superseded between
            # the upload starting and it returning) even though the upload
            # itself already committed a real, live S3 object — every
            # downstream cleanup call below gates on `archived`, so leaving
            # it False orphaned that object with no compensating delete and
            # no durable ledger record, forever.
            archived = True
            # Task 2 final consolidated backend pass (Verified Blocker 2):
            # ATOMIC, ownership-bound write — the upload itself takes real
            # time, during which ownership can change; locking the row,
            # re-verifying attempt_token, and writing all happen in ONE
            # transaction now, never a separate check-then-write with a
            # real window in between.
            applied = _atomic_ownership_write(
                db, item_id, user_id, attempt_token,
                lambda locked: (setattr(locked, "file_url", uploaded_key),
                                 setattr(locked, "archive_status", "stored")),
            )
            if not applied:
                raise AttemptOwnershipLost(item_id, attempt_token)
        except AttemptOwnershipLost:
            raise
        except Exception as e:
            # Recorded rather than only printed. `processed` alone conflates
            # three independent things — archived, extracted, indexed — so a
            # silent S3 failure left a row that looked completely fine while
            # the user's original file did not exist anywhere.
            item.archive_status = "failed"
            db.commit()
            logger.error("[process_pdf_embeddings] S3 archive FAILED for %s: %s", item_id, e)
        guard.check()

        # Paragraph-preserving extraction: story mode serves this text to the
        # reader verbatim, so the author's paragraph and dialogue breaks have to
        # survive. Joining pages with " " (what this used to do) turned every
        # book into one run-on block.
        text = pdf_to_structured_text(pdf_bytes, settings.max_extracted_text_chars)
        guard.check()

        if not text.strip():
            # Not a failure any more: a scan has no text to extract, but we can
            # read it with OCR if the user asks. The client turns 'needed' into
            # an offer rather than an error (see ocr_service). The reservation
            # stays 'pending' — the SAME attempt continues if/when the user
            # opts into OCR (reserve_free_capacity is idempotent), or the
            # reaper lazily reclaims it after inactivity if they never do,
            # exactly like any other stalled attempt.
            from app.services import ocr_service
            item.processed = False
            ocr_will_continue = ocr_service.is_available()
            if ocr_will_continue:
                item.ocr_status = "needed"
                item.ocr_pages_total = 0
                item.processing_error = None
            else:
                item.processing_error = (
                    "Couldn't read any text in this PDF — is it scanned pages/images?"
                )
            db.commit()
            if not ocr_will_continue:
                # Terminal for THIS attempt — nothing will ever pick the
                # reservation back up, so release it now rather than leaving
                # it tying up capacity until the reaper's TTL elapses.
                _release_reservation_after_failure(
                    item_id, user_id, item.processing_error,
                    lease_token=lease_token, attempt_token=attempt_token,
                    s3_key=s3_key if archived else None,
                )
            return

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "pdf"},
            attempt_token=attempt_token,
            on_batch=guard.checkpoint,
            check_ownership=guard.check,
        )
        guard.check()
        if not finalize_successful_processing(db, item, user_id, chunk_count, lease_token=lease_token, attempt_token=attempt_token):
            # Should not normally happen — capacity was already reserved
            # above — but a concurrent deletion or superseded reservation
            # can still land here; the vectors (and, if this attempt is
            # still the current owner, the archived file) must not orphan.
            _compensate_failed_attempt(
                item_id, user_id, attempt_token=attempt_token,
                s3_key=s3_key if archived else None, reason="finalize rejected",
            )
            return

        # Content is committed ONLY on this attempt's own confirmed success
        # (3rd-audit remediation #4) — assigning it earlier and letting a
        # LATER commit (e.g. inside reserve_free_capacity or the rejection
        # branch above) persist it would let a stale, ultimately-superseded
        # attempt's extracted text silently overwrite a fresher attempt's
        # already-finalized content for the same item.
        # Task 2 final consolidated backend pass (Verified Blocker 2):
        # atomic, ownership-bound write — the predicate and the write
        # share ONE locked transaction, never a separate check-then-write.
        _atomic_ownership_write(
            db, item_id, user_id, attempt_token,
            lambda locked: setattr(locked, "content", text),
        )

        # Figures, AFTER the text is safely indexed. Deliberately last and
        # deliberately swallowed: a book's pictures are a garnish on a pipeline
        # whose real job is text, and no failure here may cost the user their
        # upload. `_extract_book_images` never raises.
        _extract_book_images(db, item, pdf_bytes, user_id, attempt_token)
    except AttemptOwnershipLost as e:
        db.rollback()
        logger.warning("[process_pdf_embeddings] %s aborted — %s", item_id, e)
        _compensate_failed_attempt(
            item_id, user_id, attempt_token=attempt_token,
            s3_key=s3_key if archived else None, reason="ownership lost",
        )
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_pdf_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, EMBEDDING_DOWN_MESSAGE,
            lease_token=lease_token, attempt_token=attempt_token,
            s3_key=s3_key if archived else None,
        )
    except Exception as e:
        db.rollback()
        print(f"[process_pdf_embeddings] Error for item {item_id}: {e}")
        # Leave a readable trace on the row so the app can show what went
        # wrong instead of the item sitting in "processing" forever.
        message = f"Processing failed: {str(e)[:250]}"
        _record_processing_error(item_id, message, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, message,
            lease_token=lease_token, attempt_token=attempt_token,
            s3_key=s3_key if archived else None,
        )
    finally:
        if guard is not None:
            try:
                guard.stop()
            except GuardShutdownFailed as e:
                # Task 2 final consolidated backend pass (Verified Blocker
                # 2): a surviving guard thread is a real operational
                # problem — logged loudly, never silently swallowed — but
                # must not skip this function's own db.close() below.
                logger.error("[attempt-guard] %s", e)
        if attempt_token is not None:
            # Release the worker-attempt claim on EVERY terminal outcome —
            # idempotent (a no-op if this attempt is no longer the current
            # owner) — so a legitimate future retry can be admitted. Own
            # fresh session: `db` may be mid-rollback from a handler above.
            try:
                from app.database import SessionLocal as _SessionLocalFinal
                _db_release = _SessionLocalFinal()
                try:
                    release_worker_attempt(_db_release, item_id, user_id, attempt_token)
                finally:
                    _db_release.close()
            except Exception:
                logger.exception("failed to release worker attempt for %s", item_id)
        db.close()


def _extract_epub_text(epub_bytes: bytes) -> str:
    """Extract readable text from an EPUB (a zip of XHTML chapters).

    Proper path: META-INF/container.xml → the OPF package file → its manifest
    (id → href) + spine (reading order) → each chapter document's text.
    Fallback: every .xhtml/.html in the archive, sorted by path — still yields
    the full book when a publisher's OPF is malformed.
    No new dependency: zipfile + BeautifulSoup (already used for URL scraping).
    """
    import io
    import posixpath
    import warnings
    import zipfile
    from bs4 import BeautifulSoup

    try:  # html.parser on the OPF/container XML works fine — silence the advisory
        from bs4 import XMLParsedAsHTMLWarning
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    except ImportError:
        pass

    from app.services.text_extract import epub_doc_paragraphs, strip_front_matter

    def doc_text(raw: bytes) -> str:
        # Block-level walk, not get_text(separator="\n"): that separator fires
        # at every inline <em>/<a> too, chopping single sentences into lines.
        return "\n\n".join(epub_doc_paragraphs(raw))

    with zipfile.ZipFile(io.BytesIO(epub_bytes)) as zf:
        names = set(zf.namelist())
        ordered_docs = []
        try:
            container = BeautifulSoup(zf.read("META-INF/container.xml"), "html.parser")
            opf_path = container.find("rootfile")["full-path"]
            opf_dir = posixpath.dirname(opf_path)
            opf = BeautifulSoup(zf.read(opf_path), "html.parser")
            hrefs = {i.get("id"): i.get("href") for i in opf.find_all("item")}
            for ref in opf.find_all("itemref"):
                href = hrefs.get(ref.get("idref"))
                if not href:
                    continue
                path = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
                if path in names and path.lower().endswith((".xhtml", ".html", ".htm")):
                    ordered_docs.append(path)
        except Exception:
            ordered_docs = []
        if not ordered_docs:
            ordered_docs = sorted(
                n for n in names if n.lower().endswith((".xhtml", ".html", ".htm"))
            )

        parts = []
        for path in ordered_docs:
            try:
                t = doc_text(zf.read(path))
                if t:
                    parts.append(t)
            except Exception:
                continue
        # Drop the cover blurb / praise pages / imprint page / contents so a
        # story-mode reader's first day is the book, not its copyright notice.
        return strip_front_matter("\n\n".join(parts))


def process_epub_embeddings(item_id: str, epub_bytes: bytes, user_id: str):
    """Extract text from an EPUB in spine (reading) order, chunk, and upsert
    to Pinecone — the same pipeline as PDFs, including story-mode content.
    See process_pdf_embeddings for the reserve-before-archive/attempt-token/
    deferred-content-write rationale — identical here."""
    from app.database import SessionLocal

    db = SessionLocal()
    lease_token = None    # capacity lease — None for a 'premium' attempt
    attempt_token = None  # immutable durable attempt identity — retained
                           # for this call's ENTIRE lifetime
    s3_key = None
    archived = False
    guard = None
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        if not reserve_free_capacity(db, item, user_id):
            db.commit()
            return
        db.refresh(item)
        lease_token = item.reservation_lease_token
        # Task 2 final consolidated backend pass (Verified Blocker 1):
        # capacity reservation and worker-attempt admission are separate
        # concerns now — admit_worker_attempt mints a FRESH attempt id for
        # THIS invocation, atomically under a row lock, and rejects
        # (without touching anyone else's capacity reservation — requirement
        # 12) if a live attempt already owns the item or it is already fully
        # processed (a replay after success).
        attempt_token = admit_worker_attempt(db, item_id, user_id)
        if attempt_token is None:
            logger.info("[process_epub_embeddings] %s: worker-attempt admission rejected "
                        "(already live or already processed)", item_id)
            return
        # Attempt-scoped archive key — see process_pdf_embeddings.
        s3_key = f"{user_id}/{item_id}/{attempt_token}.epub"

        guard = _AttemptGuard(item_id, user_id, lease_token, attempt_token)
        guard.start()

        # Best-effort archive of the original file (same as PDFs)
        guard.check()
        try:
            s3 = S3Service()
            uploaded_key = s3.upload_file(
                file_content=epub_bytes,
                filename=s3_key,
                content_type="application/epub+zip",
            )
            # `archived` must track whether the S3 OBJECT itself was
            # actually written, not whether the row write that follows
            # also succeeded — see process_pdf_embeddings's matching note
            # (Section O, real repro: a genuinely-uploaded object was
            # silently orphaned because this flag was set only after a
            # row write that can legitimately fail with
            # AttemptOwnershipLost on its own).
            archived = True
            # Atomic, ownership-bound write — see process_pdf_embeddings for
            # why a separate check-then-write is not sufficient here.
            applied = _atomic_ownership_write(
                db, item_id, user_id, attempt_token,
                lambda locked: (setattr(locked, "file_url", uploaded_key),
                                 setattr(locked, "archive_status", "stored")),
            )
            if not applied:
                raise AttemptOwnershipLost(item_id, attempt_token)
        except AttemptOwnershipLost:
            raise
        except Exception as e:
            item.archive_status = "failed"
            db.commit()
            logger.error("[process_epub_embeddings] S3 archive FAILED for %s: %s", item_id, e)
        guard.check()

        text = _extract_epub_text(epub_bytes)
        text = text[: settings.max_extracted_text_chars]
        guard.check()

        if not text.strip():
            item.processed = False
            item.processing_error = "Couldn't read any text in this EPUB — the file may be DRM-protected."
            db.commit()
            _release_reservation_after_failure(
                item_id, user_id, item.processing_error,
                lease_token=lease_token, attempt_token=attempt_token,
                s3_key=s3_key if archived else None,
            )
            return

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "epub"},
            attempt_token=attempt_token,
            on_batch=guard.checkpoint,
            check_ownership=guard.check,
        )
        guard.check()
        if not finalize_successful_processing(db, item, user_id, chunk_count, lease_token=lease_token, attempt_token=attempt_token):
            # See the PDF path's identical comment — compensate rather than
            # orphan the vectors (and archive) just written.
            _compensate_failed_attempt(
                item_id, user_id, attempt_token=attempt_token,
                s3_key=s3_key if archived else None, reason="finalize rejected",
            )
            return

        # Content committed only on this attempt's own confirmed success —
        # see process_pdf_embeddings for why.
        # Task 2 final consolidated backend pass (Verified Blocker 2):
        # atomic, ownership-bound write — the predicate and the write
        # share ONE locked transaction, never a separate check-then-write.
        _atomic_ownership_write(
            db, item_id, user_id, attempt_token,
            lambda locked: setattr(locked, "content", text),
        )

        # Figures, after the text is safely indexed — see the PDF path. An
        # EPUB's images come with captions and alt text, so they describe
        # themselves far better than a PDF's do.
        _extract_book_images(db, item, epub_bytes, user_id, attempt_token)
    except AttemptOwnershipLost as e:
        db.rollback()
        logger.warning("[process_epub_embeddings] %s aborted — %s", item_id, e)
        _compensate_failed_attempt(
            item_id, user_id, attempt_token=attempt_token,
            s3_key=s3_key if archived else None, reason="ownership lost",
        )
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_epub_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, EMBEDDING_DOWN_MESSAGE,
            lease_token=lease_token, attempt_token=attempt_token,
            s3_key=s3_key if archived else None,
        )
    except Exception as e:
        db.rollback()
        print(f"[process_epub_embeddings] Error for item {item_id}: {e}")
        message = f"Processing failed: {str(e)[:250]}"
        _record_processing_error(item_id, message, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, message,
            lease_token=lease_token, attempt_token=attempt_token,
            s3_key=s3_key if archived else None,
        )
    finally:
        if guard is not None:
            try:
                guard.stop()
            except GuardShutdownFailed as e:
                # Task 2 final consolidated backend pass (Verified Blocker
                # 2): a surviving guard thread is a real operational
                # problem — logged loudly, never silently swallowed — but
                # must not skip this function's own db.close() below.
                logger.error("[attempt-guard] %s", e)
        if attempt_token is not None:
            # Release the worker-attempt claim on EVERY terminal outcome —
            # idempotent (a no-op if this attempt is no longer the current
            # owner) — so a legitimate future retry can be admitted. Own
            # fresh session: `db` may be mid-rollback from a handler above.
            try:
                from app.database import SessionLocal as _SessionLocalFinal
                _db_release = _SessionLocalFinal()
                try:
                    release_worker_attempt(_db_release, item_id, user_id, attempt_token)
                finally:
                    _db_release.close()
            except Exception:
                logger.exception("failed to release worker attempt for %s", item_id)
        db.close()


def process_url_embeddings(item_id: str, url: str, user_id: str):
    """Scrape URL content, extract readable text, chunk, and upsert to
    Pinecone. Task 2 remediation (3rd audit, reserve-before-side-effect #3):
    reservation now happens BEFORE the network fetch — the SSRF-guarded
    fetch itself is real outbound network work an over-capacity account
    must never pay for. `validate_public_url`'s local/DNS-shape check
    already ran synchronously in the router before this background task was
    even scheduled (see add_url) — that's the one genuinely local,
    side-effect-free check allowed ahead of reservation; the actual fetch
    stays here, after it."""
    from app.database import SessionLocal
    from bs4 import BeautifulSoup

    db = SessionLocal()
    lease_token = None    # capacity lease — None for a 'premium' attempt
    attempt_token = None  # immutable durable attempt identity — retained
                           # for this call's ENTIRE lifetime
    guard = None
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        if not reserve_free_capacity(db, item, user_id):
            db.commit()
            return
        db.refresh(item)
        lease_token = item.reservation_lease_token
        # Task 2 final consolidated backend pass (Verified Blocker 1):
        # capacity reservation and worker-attempt admission are separate
        # concerns now — admit_worker_attempt mints a FRESH attempt id for
        # THIS invocation, atomically under a row lock, and rejects
        # (without touching anyone else's capacity reservation — requirement
        # 12) if a live attempt already owns the item or it is already fully
        # processed (a replay after success).
        attempt_token = admit_worker_attempt(db, item_id, user_id)
        if attempt_token is None:
            logger.info("[process_url_embeddings] %s: worker-attempt admission rejected "
                        "(already live or already processed)", item_id)
            return

        # Full-attempt heartbeat starts the instant the reservation commits —
        # covers the SSRF-guarded fetch and pre-first-batch embedding-
        # generation phases below, neither of which has a natural per-
        # operation checkpoint of its own.
        guard = _AttemptGuard(item_id, user_id, lease_token, attempt_token)
        guard.start()

        headers = {"User-Agent": "Mozilla/5.0 (compatible; Nibbler/1.0)"}
        # SSRF-guarded fetch: validates every redirect hop, caps download size
        guard.check()
        response = fetch_public_url(url, headers=headers, timeout=15)
        guard.check()

        soup = BeautifulSoup(response.text, "html.parser")

        # Remove boilerplate tags
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "iframe"]):
            tag.decompose()

        # Try to extract main article content first
        main = (
            soup.find("article")
            or soup.find("main")
            or soup.find(id="content")
            or soup.find(class_="content")
            or soup.find(class_="post-content")
            or soup.find(class_="entry-content")
            or soup.body
        )

        text = main.get_text(separator=" ", strip=True) if main else soup.get_text(separator=" ", strip=True)
        text = text[: settings.max_extracted_text_chars]

        # Auto-set title from page <title> if not provided — cosmetic, not
        # extracted "content", safe to keep regardless of eventual outcome.
        if item.title == url:
            page_title = soup.find("title")
            if page_title:
                item.title = page_title.get_text(strip=True)[:200]

        if not text.strip():
            # Authoritative re-check immediately before this write (Task 2
            # consolidated backend pass) — the fetch itself is real
            # network time during which ownership can change; letting
            # AttemptOwnershipLost propagate here (uncaught in this
            # block) routes to the function's own outer handler, which
            # runs the correct compensating cleanup instead of this
            # empty-content branch's own release path.
            guard.check()
            item.processed = False
            item.processing_error = "Could not extract text from URL."
            db.commit()
            _release_reservation_after_failure(
                item_id, user_id, item.processing_error,
                lease_token=lease_token, attempt_token=attempt_token,
            )
            return

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "url", "source_url": url},
            attempt_token=attempt_token,
            on_batch=guard.checkpoint,
            check_ownership=guard.check,
        )
        guard.check()
        if not finalize_successful_processing(db, item, user_id, chunk_count, lease_token=lease_token, attempt_token=attempt_token):
            _compensate_failed_attempt(
                item_id, user_id, attempt_token=attempt_token, reason="finalize rejected",
            )
            return

        # Content committed only on this attempt's own confirmed success —
        # see process_pdf_embeddings for the cross-attempt race this avoids.
        # Task 2 final consolidated backend pass (Verified Blocker 2):
        # atomic, ownership-bound write — the predicate and the write
        # share ONE locked transaction, never a separate check-then-write.
        _atomic_ownership_write(
            db, item_id, user_id, attempt_token,
            lambda locked: setattr(locked, "content", text),
        )
    except AttemptOwnershipLost as e:
        db.rollback()
        logger.warning("[process_url_embeddings] %s aborted — %s", item_id, e)
        _compensate_failed_attempt(item_id, user_id, attempt_token=attempt_token, reason="ownership lost")
    except UnsafeUrlError as e:
        # A redirect hop pointed somewhere non-public (or the page was too
        # large) — surface it on the row instead of leaving it "processing".
        db.rollback()
        message = str(e)
        _record_processing_error(item_id, message, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, message, lease_token=lease_token, attempt_token=attempt_token,
        )
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_url_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, EMBEDDING_DOWN_MESSAGE, lease_token=lease_token, attempt_token=attempt_token,
        )
    except Exception as e:
        db.rollback()
        print(f"[process_url_embeddings] Error for item {item_id}: {e}")
        message = f"Processing failed: {str(e)[:250]}"
        _record_processing_error(item_id, message, attempt_token)
        _release_reservation_after_failure(
            item_id, user_id, message,
            lease_token=lease_token, attempt_token=attempt_token,
        )
    finally:
        if guard is not None:
            try:
                guard.stop()
            except GuardShutdownFailed as e:
                # Task 2 final consolidated backend pass (Verified Blocker
                # 2): a surviving guard thread is a real operational
                # problem — logged loudly, never silently swallowed — but
                # must not skip this function's own db.close() below.
                logger.error("[attempt-guard] %s", e)
        if attempt_token is not None:
            # Release the worker-attempt claim on EVERY terminal outcome —
            # idempotent (a no-op if this attempt is no longer the current
            # owner) — so a legitimate future retry can be admitted. Own
            # fresh session: `db` may be mid-rollback from a handler above.
            try:
                from app.database import SessionLocal as _SessionLocalFinal
                _db_release = _SessionLocalFinal()
                try:
                    release_worker_attempt(_db_release, item_id, user_id, attempt_token)
                finally:
                    _db_release.close()
            except Exception:
                logger.exception("failed to release worker attempt for %s", item_id)
        db.close()
