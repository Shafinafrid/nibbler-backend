from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite, SavedBite
from app.models.user_data import ChatMessage, Completion, Highlight, Note
from app.rate_limit import limiter
from app.schemas.library import LibraryItemCreate, LibraryItemResponse, LibraryItemList, LibraryItemUrlCreate, SetActiveRequest, UpdateItemRequest
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService, EmbeddingError
from app.services.url_safety import UnsafeUrlError, validate_public_url, fetch_public_url
from app.config import get_settings
import logging
import uuid

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
    if not user.effective_premium:
        count = db.query(LibraryItem).filter(LibraryItem.user_id == user.id).count()
        if count >= settings.free_upload_limit:
            raise HTTPException(
                status_code=403,
                detail=f"Free plan allows {settings.free_upload_limit} library items. Upgrade to Premium for unlimited uploads.",
            )


# ── GET /library/ ─────────────────────────────────────────────────────────────
@router.get("/", response_model=LibraryItemList)
def list_library(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = (
        db.query(LibraryItem)
        .filter(LibraryItem.user_id == current_user.id)
        .order_by(LibraryItem.created_at.desc())
        .all()
    )
    count = len(items)
    return LibraryItemList(
        items=items,
        total=count,
        limit_reached=not current_user.effective_premium and count >= settings.free_upload_limit,
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
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(process_item_embeddings, item.id, current_user.id)
    return item


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
    return item


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
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(process_url_embeddings, item.id, data.url, current_user.id)
    return item


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
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")

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
    db.commit()
    db.refresh(item)
    return item


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
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")

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
    return item

def _run_ocr(item_id: str, user_id: str):
    """Read a scanned PDF and push it through the normal indexing path.

    Runs in a BackgroundTask, and ocr_service serialises these so only one
    holds the CPU at a time — see that module for why that guard matters more
    than the cost does.
    """
    from app.database import SessionLocal
    from app.services import ocr_service
    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(
            LibraryItem.id == item_id, LibraryItem.user_id == user_id
        ).first()
        if not item or not item.file_url:
            return

        item.ocr_status = "running"
        item.ocr_pages_done = 0
        db.commit()

        pdf_bytes = S3Service().download_file(item.file_url)

        def progress(done: int, total: int):
            # Committed as it goes so the Library row can show "page 40 of 335"
            # — a job this long with only a spinner reads as broken.
            item.ocr_pages_done = done
            item.ocr_pages_total = total
            db.commit()

        text = ocr_service.ocr_pdf(pdf_bytes, on_progress=progress)

        item.content = text
        chunk_count = EmbeddingService().index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "pdf"},
        )
        item.chunk_count = chunk_count
        item.processed = True
        item.ocr_status = "done"
        item.processing_error = None
        db.commit()
        logger.info("[ocr] %s indexed %s chunks from %s pages", item_id, chunk_count, item.ocr_pages_total)
    except Exception as e:
        db.rollback()
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item:
                item.ocr_status = "failed"
                item.processing_error = str(e)[:400]
                db.commit()
        except Exception:
            pass
        logger.error("[ocr] FAILED for %s: %s", item_id, e)
    finally:
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
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")
    if not ocr_service.is_available():
        raise HTTPException(status_code=503, detail={
            "code": "ocr_unavailable",
            "message": "Nibbler can't read scanned books just now. Please try again later.",
        })
    if item.ocr_status == "running":
        return item
    if not item.file_url:
        raise HTTPException(status_code=409, detail={
            "code": "no_original",
            "message": "The original file isn't stored, so it can't be re-read.",
        })

    item.ocr_status = "running"
    item.ocr_pages_done = 0
    item.processing_error = None
    db.commit()
    db.refresh(item)
    background_tasks.add_task(_run_ocr, item.id, current_user.id)
    return item

# ── DELETE /library/{item_id} ──────────────────────────────────────────────────
# One hour. Long enough that a session's images are all fetched under one URL,
# short enough that a leaked link is worthless by the time it travels.
IMAGE_URL_TTL = 3600


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
    """Mint a short-lived view URL for one extracted book figure, for its owner.

    Returns JSON — `{"url": ..., "expires_in": 3600}` — deliberately, NOT a 307
    redirect to S3. A redirect would have the client following a cross-host hop
    while holding an `Authorization: Bearer <firebase id token>` header, and
    iOS's URLSession forwards headers across redirects by default. That would
    hand a user's Firebase token to Amazon on every image load. Returning the
    URL as data keeps the authenticated request and the storage request
    completely separate: the app fetches this with its token, then fetches the
    picture with no credentials at all.

    What a card persists is the API PATH, not the URL this returns. Presigned
    URLs expire in an hour and a nibble is replayed from the Nibble Bank months
    later, so a persisted URL would be a card that works today and 404s in
    August. Refreshing expired access is therefore just calling this again.

    Ownership is established by the QUERY, not by comparing ids: the lookup is
    scoped to this user AND this book, so an id belonging to another account is
    simply not found. There is no comparison to get wrong, and enumeration
    reveals nothing.

    Scoping by BOOK as well as owner matters because candidate ids were once
    derived from the image checksum alone: the same figure in two uploaded
    books produced one id, and a library-wide search then had two rows to
    choose from and picked whichever came first. The path names the book, and
    the id is now salted with it too.
    """
    if not candidate_id or not candidate_id.startswith("img_") or len(candidate_id) > 64:
        raise HTTPException(status_code=404, detail="Image not found.")

    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == current_user.id,
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="Image not found.")

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
            url = S3Service().generate_presigned_url(key, expiry=IMAGE_URL_TTL)
        except Exception as e:
            logger.warning("Presign failed for %s: %s", candidate_id, e)
            raise HTTPException(status_code=502, detail="Image unavailable right now.")
        return {
            "id": candidate_id,
            "item_id": item.id,
            "url": url,
            "expires_in": IMAGE_URL_TTL,
            "mime": img.get("mime") or "image/png",
            "alt": img.get("alt") or img.get("caption") or "",
            "w": img.get("w"),
            "h": img.get("h"),
        }

    raise HTTPException(status_code=404, detail="Image not found.")


@router.delete("/{item_id}")
def delete_library_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == current_user.id,
    ).first()

    if not item:
        raise HTTPException(status_code=404, detail="Item not found.")

    embedding_svc = EmbeddingService()
    vectors_cleared = embedding_svc.delete_item_vectors(item_id, user_id=current_user.id)

    file_cleared = True
    if item.file_url:
        file_cleared = S3Service().delete_file(item.file_url)

    # Extracted figures are separate S3 objects from the source file, so they
    # survive deleting the book unless deleted explicitly. Scoped to this
    # owner and this book: the keys come from the stored rows, never from user
    # input, so no path here can address another user's objects.
    images_cleared = _delete_item_images(item, current_user.id)

    # Everything derived from this book goes with it, in the same transaction.
    #
    # None of these tables has a foreign key to library_items — daily_bites
    # carries a plain `library_item_id` string, and notes/highlights/chats/
    # completions carry a bare `book_id` — so nothing cascaded and every one of
    # them survived the delete. The consequences were real: notes and highlights
    # for books that no longer exist were restored onto every new device
    # forever with nothing behind them, orphaned decks kept their full cards and
    # quizzes indefinitely, and `GET /bites/sessions` happily served them back.
    # The app's own confirmation copy promises the book goes "along with its
    # stored content", so hard delete is the behaviour that matches the promise.
    # saved_bites has a real FK to daily_bites with ON DELETE CASCADE, but that
    # is enforced by the DATABASE — and relying on it would make this behaviour
    # depend on engine settings rather than on this code. Deleted explicitly so
    # it is true everywhere and provable in a test.
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

    db.delete(item)
    db.commit()

    if not (vectors_cleared and file_cleared and images_cleared):
        # The row is gone either way — but say so, rather than reporting a clean
        # delete while objects or vectors are still out there.
        logger.error(
            "Library delete incomplete for item %s (user %s): pinecone_ok=%s s3_ok=%s "
            "images_ok=%s. Manual cleanup required.",
            item_id, current_user.id, vectors_cleared, file_cleared, images_cleared,
        )

    return {
        "message": "Item deleted successfully",
        "removed": removed,
        "external_cleanup_complete": bool(vectors_cleared and file_cleared and images_cleared),
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


def _extract_book_images(db, item, file_bytes: bytes, user_id: str) -> int:
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
    """
    from app.services.image_extract import extract_and_store, pdf_page_texts

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
            item_id=item.id,
            user_id=user_id,
            page_texts=page_texts,
        )
        if not images:
            return 0
        try:
            item.images = images
            db.commit()
        except Exception as e:
            # The objects are already in S3. Failing to record them would leave
            # paid-for files nothing knows about — invisible to book deletion,
            # invisible to account erasure, and therefore permanent. Delete
            # what we just uploaded rather than orphan it.
            logger.error("[images] could not persist rows for %s (%s) — removing "
                         "the uploaded objects", item.id, e)
            try:
                db.rollback()
            except Exception:
                pass
            from app.services.image_extract import delete_stored
            delete_stored(images)
            return 0
        return len(images)
    except Exception as e:
        logger.warning("[images] extraction skipped for %s: %s", item.id, e)
        try:
            db.rollback()
        except Exception:
            pass
        return 0


def _record_processing_error(item_id: str, message: str):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if item:
            item.processing_error = message[:250]
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def process_item_embeddings(item_id: str, user_id: str):
    """Chunk plain-text / pasted content and upsert to Pinecone."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item or not item.content:
            return

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=item.content,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": item.type},
        )
        item.processed = True
        item.chunk_count = chunk_count
        db.commit()
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_item_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE)
    except Exception as e:
        # Without this the row sat processed=False forever with no error —
        # the app polled endlessly with nothing to show the user.
        db.rollback()
        print(f"[process_item_embeddings] Error for item {item_id}: {e}")
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item:
                item.processing_error = f"Processing failed: {str(e)[:250]}"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def process_pdf_embeddings(item_id: str, pdf_bytes: bytes, user_id: str):
    """Extract text from the uploaded PDF bytes, chunk, and upsert to
    Pinecone. Works straight from the request payload — no S3 round-trip,
    so processing succeeds even when file archival is unavailable."""
    from app.database import SessionLocal
    from app.services.text_extract import pdf_to_structured_text

    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        # Best-effort archive of the original file (needs AWS keys on Railway;
        # skipped silently when unavailable — nothing downstream depends on it)
        try:
            s3 = S3Service()
            item.file_url = s3.upload_file(
                file_content=pdf_bytes,
                filename=f"{user_id}/{item_id}.pdf",
                content_type="application/pdf",
            )
            item.archive_status = "stored"
            db.commit()
        except Exception as e:
            # Recorded rather than only printed. `processed` alone conflates
            # three independent things — archived, extracted, indexed — so a
            # silent S3 failure left a row that looked completely fine while
            # the user's original file did not exist anywhere.
            item.archive_status = "failed"
            db.commit()
            logger.error("[process_pdf_embeddings] S3 archive FAILED for %s: %s", item_id, e)

        # Paragraph-preserving extraction: story mode serves this text to the
        # reader verbatim, so the author's paragraph and dialogue breaks have to
        # survive. Joining pages with " " (what this used to do) turned every
        # book into one run-on block.
        text = pdf_to_structured_text(pdf_bytes, settings.max_extracted_text_chars)

        if not text.strip():
            # Not a failure any more: a scan has no text to extract, but we can
            # read it with OCR if the user asks. The client turns 'needed' into
            # an offer rather than an error (see ocr_service).
            from app.services import ocr_service
            item.processed = False
            if ocr_service.is_available():
                item.ocr_status = "needed"
                item.ocr_pages_total = 0
                item.processing_error = None
            else:
                item.processing_error = (
                    "Couldn't read any text in this PDF — is it scanned pages/images?"
                )
            db.commit()
            return

        # Keep the full extracted text on the row — story mode reads the book
        # sequentially from here.
        item.content = text

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "pdf"},
        )
        item.processed = True
        item.chunk_count = chunk_count
        db.commit()

        # Figures, AFTER the text is safely indexed. Deliberately last and
        # deliberately swallowed: a book's pictures are a garnish on a pipeline
        # whose real job is text, and no failure here may cost the user their
        # upload. `_extract_book_images` never raises.
        _extract_book_images(db, item, pdf_bytes, user_id)
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_pdf_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE)
    except Exception as e:
        db.rollback()
        print(f"[process_pdf_embeddings] Error for item {item_id}: {e}")
        # Leave a readable trace on the row so the app can show what went
        # wrong instead of the item sitting in "processing" forever.
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item:
                item.processing_error = f"Processing failed: {str(e)[:250]}"
                db.commit()
        except Exception:
            pass
    finally:
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
    to Pinecone — the same pipeline as PDFs, including story-mode content."""
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        # Best-effort archive of the original file (same as PDFs)
        try:
            s3 = S3Service()
            item.file_url = s3.upload_file(
                file_content=epub_bytes,
                filename=f"{user_id}/{item_id}.epub",
                content_type="application/epub+zip",
            )
            item.archive_status = "stored"
            db.commit()
        except Exception as e:
            item.archive_status = "failed"
            db.commit()
            logger.error("[process_epub_embeddings] S3 archive FAILED for %s: %s", item_id, e)

        text = _extract_epub_text(epub_bytes)
        text = text[: settings.max_extracted_text_chars]

        if not text.strip():
            item.processed = False
            item.processing_error = "Couldn't read any text in this EPUB — the file may be DRM-protected."
            db.commit()
            return

        # Full text on the row — story mode reads the book sequentially from here.
        item.content = text

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "epub"},
        )
        item.processed = True
        item.chunk_count = chunk_count
        db.commit()

        # Figures, after the text is safely indexed — see the PDF path. An
        # EPUB's images come with captions and alt text, so they describe
        # themselves far better than a PDF's do.
        _extract_book_images(db, item, epub_bytes, user_id)
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_epub_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE)
    except Exception as e:
        db.rollback()
        print(f"[process_epub_embeddings] Error for item {item_id}: {e}")
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item:
                item.processing_error = f"Processing failed: {str(e)[:250]}"
                db.commit()
        except Exception:
            pass
    finally:
        db.close()


def process_url_embeddings(item_id: str, url: str, user_id: str):
    """Scrape URL content, extract readable text, chunk, and upsert to Pinecone."""
    from app.database import SessionLocal
    from bs4 import BeautifulSoup

    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        headers = {"User-Agent": "Mozilla/5.0 (compatible; Nibbler/1.0)"}
        # SSRF-guarded fetch: validates every redirect hop, caps download size
        response = fetch_public_url(url, headers=headers, timeout=15)

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

        # Auto-set title from page <title> if not provided
        if item.title == url:
            page_title = soup.find("title")
            if page_title:
                item.title = page_title.get_text(strip=True)[:200]

        if not text.strip():
            item.processed = False
            item.processing_error = "Could not extract text from URL."
            db.commit()
            return

        # Full text on the row so story mode can read sequentially
        item.content = text

        embedding_svc = EmbeddingService()
        chunk_count = embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "url", "source_url": url},
        )
        item.processed = True
        item.chunk_count = chunk_count
        db.commit()
    except UnsafeUrlError as e:
        # A redirect hop pointed somewhere non-public (or the page was too
        # large) — surface it on the row instead of leaving it "processing".
        db.rollback()
        try:
            item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
            if item:
                item.processing_error = str(e)
                db.commit()
        except Exception:
            pass
    except EmbeddingError as e:
        db.rollback()
        print(f"[process_url_embeddings] Embedding failed for item {item_id}: {e}")
        _record_processing_error(item_id, EMBEDDING_DOWN_MESSAGE)
    except Exception as e:
        db.rollback()
        print(f"[process_url_embeddings] Error for item {item_id}: {e}")
    finally:
        db.close()
