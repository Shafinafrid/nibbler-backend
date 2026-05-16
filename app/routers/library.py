from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.schemas.library import LibraryItemCreate, LibraryItemResponse, LibraryItemList
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService
from app.config import get_settings
import uuid

router = APIRouter(prefix="/library", tags=["library"])
settings = get_settings()


def check_upload_limit(user: User, db: Session):
    if not user.is_premium:
        count = db.query(LibraryItem).filter(LibraryItem.user_id == user.id).count()
        if count >= settings.free_upload_limit:
            raise HTTPException(
                status_code=403,
                detail=f"Free plan allows {settings.free_upload_limit} library items. Upgrade to Premium for unlimited uploads.",
            )


@router.get("/", response_model=LibraryItemList)
async def list_library(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = db.query(LibraryItem).filter(LibraryItem.user_id == current_user.id).order_by(LibraryItem.created_at.desc()).all()
    count = len(items)
    return LibraryItemList(
        items=items,
        total=count,
        limit_reached=not current_user.is_premium and count >= settings.free_upload_limit,
    )


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
        processed=False,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    # Process embeddings in background
    background_tasks.add_task(process_item_embeddings, item.id, current_user.id)
    return item


@router.post("/upload-pdf", response_model=LibraryItemResponse)
async def upload_pdf(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    check_upload_limit(current_user, db)

    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    # Upload to S3
    s3 = S3Service()
    file_content = await file.read()
    file_url = await s3.upload_file(
        file_content=file_content,
        filename=f"{current_user.id}/{uuid.uuid4()}.pdf",
        content_type="application/pdf",
    )

    item = LibraryItem(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        title=file.filename.replace(".pdf", ""),
        type="pdf",
        file_url=file_url,
        file_size=len(file_content),
        processed=False,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(process_pdf_embeddings, item.id, file_url, current_user.id)
    return item


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

    # Delete from Pinecone
    embedding_svc = EmbeddingService()
    await embedding_svc.delete_item_vectors(item_id)

    # Delete from S3 if PDF
    if item.file_url:
        s3 = S3Service()
        await s3.delete_file(item.file_url)

    db.delete(item)
    db.commit()
    return {"message": "Item deleted successfully"}


async def process_item_embeddings(item_id: str, user_id: str):
    """Background task: chunk text content and upsert to Pinecone."""
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


async def process_pdf_embeddings(item_id: str, file_url: str, user_id: str):
    """Background task: extract text from PDF, chunk, and upsert to Pinecone."""
    from app.database import SessionLocal
    from app.services.s3_service import S3Service
    import PyPDF2
    import io

    db = SessionLocal()
    try:
        item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
        if not item:
            return

        s3 = S3Service()
        pdf_bytes = await s3.download_file(file_url)
        reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
        text = " ".join(page.extract_text() or "" for page in reader.pages)

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
    finally:
        db.close()
