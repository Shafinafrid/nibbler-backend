from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks, Request
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.rate_limit import limiter
from app.schemas.library import LibraryItemCreate, LibraryItemResponse, LibraryItemList, LibraryItemUrlCreate, SetActiveRequest
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService, EmbeddingError
from app.services.url_safety import UnsafeUrlError, validate_public_url, fetch_public_url
from app.config import get_settings
import uuid

router = APIRouter(prefix="/library", tags=["library"])
settings = get_settings()

MAX_ACTIVE_SOURCES = 5  # mirrors MAX_ACTIVE_BOOKS in nibbler/src/data/sessionStore.js


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

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    max_bytes = settings.max_pdf_upload_mb * 1024 * 1024
    too_large = HTTPException(
        status_code=413,
        detail=f"PDFs up to {settings.max_pdf_upload_mb} MB are supported — this file is larger.",
    )
    # Fast reject on the declared size, then enforce for real while reading in
    # chunks — one unbounded read() of a huge file can OOM the whole server.
    if file.size and file.size > max_bytes:
        raise too_large

    chunks, size = [], 0
    # Sync handler (runs in FastAPI's threadpool) — read the spooled temp file
    # via the underlying file object.
    while chunk := file.file.read(1024 * 1024):
        size += len(chunk)
        if size > max_bytes:
            raise too_large
        chunks.append(chunk)
    file_content = b"".join(chunks)
    if not file_content:
        raise HTTPException(status_code=400, detail="That file appears to be empty.")

    # Respond as soon as the bytes have arrived — S3 archival AND text
    # extraction/embedding all happen in the background task, so the app
    # never waits on Claude, Pinecone, or a slow/broken AWS setup.
    item = LibraryItem(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        title=(title or file.filename.replace(".pdf", "").replace(".PDF", "")).strip(),
        type="pdf",
        file_url=None,
        file_size=len(file_content),
        mode=mode or "wisdom",
        kind=kind or "book",
        author=author,
        growth_profile_name=growth_profile_name if (mode or "wisdom") == "wisdom" else None,
        processed=False,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(process_pdf_embeddings, item.id, file_content, current_user.id)
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


# ── DELETE /library/{item_id} ──────────────────────────────────────────────────
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
    embedding_svc.delete_item_vectors(item_id, user_id=current_user.id)

    if item.file_url:
        s3 = S3Service()
        s3.delete_file(item.file_url)

    db.delete(item)
    db.commit()
    return {"message": "Item deleted successfully"}


# ── Background tasks ───────────────────────────────────────────────────────────

# Shown on the library row when Voyage rejects the embedding batches. Before
# July 2026 this failure was silently swallowed into random mock vectors, which
# poisoned Pinecone and made the Connect goal-match read ~4% forever. Failing
# loudly is the correct behavior.
EMBEDDING_DOWN_MESSAGE = (
    "Nibbler couldn't finish reading this one — the reading service is briefly "
    "unavailable. Delete it and upload again in a few minutes."
)


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
    import PyPDF2
    import io

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
            db.commit()
        except Exception as e:
            print(f"[process_pdf_embeddings] S3 archive skipped: {e}")

        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join(page.extract_text() or "" for page in reader.pages)
        text = text[: settings.max_extracted_text_chars]

        if not text.strip():
            item.processed = False
            item.processing_error = "Couldn't read any text in this PDF — is it scanned pages/images?"
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
