from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, BackgroundTasks
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.schemas.library import LibraryItemCreate, LibraryItemResponse, LibraryItemList, LibraryItemUrlCreate
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService
from app.config import get_settings
import uuid

router = APIRouter(prefix="/library", tags=["library"])
settings = get_settings()


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
async def list_library(
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
async def add_library_item(
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
async def upload_pdf(
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

    file_content = await file.read()
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
async def add_url(
    data: LibraryItemUrlCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Scrape an article/blog URL and add its content to the library."""
    check_upload_limit(current_user, db)

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


# ── DELETE /library/{item_id} ──────────────────────────────────────────────────
@router.delete("/{item_id}")
async def delete_library_item(
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
    await embedding_svc.delete_item_vectors(item_id, user_id=current_user.id)

    if item.file_url:
        s3 = S3Service()
        await s3.delete_file(item.file_url)

    db.delete(item)
    db.commit()
    return {"message": "Item deleted successfully"}


# ── Background tasks ───────────────────────────────────────────────────────────

async def process_item_embeddings(item_id: str, user_id: str):
    """Chunk plain-text / pasted content and upsert to Pinecone."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item or not item.content:
            return

        embedding_svc = EmbeddingService()
        chunk_count = await embedding_svc.index_text(
            text=item.content,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": item.type},
        )
        item.processed = True
        item.chunk_count = chunk_count
        db.commit()
    finally:
        db.close()


async def process_pdf_embeddings(item_id: str, pdf_bytes: bytes, user_id: str):
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
            item.file_url = await s3.upload_file(
                file_content=pdf_bytes,
                filename=f"{user_id}/{item_id}.pdf",
                content_type="application/pdf",
            )
            db.commit()
        except Exception as e:
            print(f"[process_pdf_embeddings] S3 archive skipped: {e}")

        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join(page.extract_text() or "" for page in reader.pages)

        if not text.strip():
            item.processed = False
            item.processing_error = "Couldn't read any text in this PDF — is it scanned pages/images?"
            db.commit()
            return

        # Keep the full extracted text on the row — story mode reads the book
        # sequentially from here.
        item.content = text

        embedding_svc = EmbeddingService()
        chunk_count = await embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "pdf"},
        )
        item.processed = True
        item.chunk_count = chunk_count
        db.commit()
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


async def process_url_embeddings(item_id: str, url: str, user_id: str):
    """Scrape URL content, extract readable text, chunk, and upsert to Pinecone."""
    from app.database import SessionLocal
    import requests
    from bs4 import BeautifulSoup

    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        headers = {"User-Agent": "Mozilla/5.0 (compatible; Nibbler/1.0)"}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()

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
        chunk_count = await embedding_svc.index_text(
            text=text,
            item_id=item_id,
            user_id=user_id,
            metadata={"title": item.title, "type": "url", "source_url": url},
        )
        item.processed = True
        item.chunk_count = chunk_count
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[process_url_embeddings] Error for item {item_id}: {e}")
    finally:
        db.close()
