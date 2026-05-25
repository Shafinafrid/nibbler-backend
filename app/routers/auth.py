import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user, verify_firebase_token, get_or_create_user
from app.models.user import User
from app.models.library import LibraryItem
from app.schemas.user import UserResponse
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService
from app.services import mixpanel_service
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer()


@router.post("/verify", response_model=UserResponse)
async def verify_and_login(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """Verify Firebase token and return/create user. Called when the app starts or after sign-in."""
    decoded = verify_firebase_token(credentials.credentials)
    user = get_or_create_user(decoded, db)
    return user


@router.get("/me", response_model=UserResponse)
async def get_me(current_user: User = Depends(get_current_user)):
    """Return the current authenticated user."""
    return current_user


@router.delete("/me")
async def delete_account(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Permanently delete the user account and all associated data.
    GDPR Article 17 — Right to Erasure.

    Deletion order:
    1. S3 files (PDFs uploaded by the user)
    2. Pinecone vectors (entire user namespace)
    3. PostgreSQL rows (CASCADE handles all child tables)
    4. Firebase Auth account
    5. Analytics event
    """
    user_id = current_user.id

    # ── 1. Delete S3 files ────────────────────────────────────────────────────
    try:
        s3 = S3Service()
        library_items = db.query(LibraryItem).filter(
            LibraryItem.user_id == user_id,
            LibraryItem.file_url.isnot(None),
        ).all()
        for item in library_items:
            await s3.delete_file(item.file_url)
        logger.info("Deleted %d S3 files for user %s", len(library_items), user_id)
    except Exception as e:
        logger.error("S3 deletion failed for user %s: %s", user_id, e)
        # Continue — partial failure should not block account deletion

    # ── 2. Delete Pinecone vectors ────────────────────────────────────────────
    try:
        embeddings = EmbeddingService()
        await embeddings.delete_user_namespace(user_id)
        logger.info("Deleted Pinecone namespace for user %s", user_id)
    except Exception as e:
        logger.error("Pinecone deletion failed for user %s: %s", user_id, e)

    # ── 3. Delete from PostgreSQL (CASCADE handles all child tables) ──────────
    db.delete(current_user)
    db.commit()
    logger.info("Deleted PostgreSQL records for user %s", user_id)

    # ── 4. Delete Firebase Auth account ──────────────────────────────────────
    try:
        import firebase_admin.auth as firebase_auth
        firebase_auth.delete_user(user_id)
        logger.info("Deleted Firebase account for user %s", user_id)
    except Exception as e:
        logger.error("Firebase account deletion failed for user %s: %s", user_id, e)

    # ── 5. Track analytics ────────────────────────────────────────────────────
    await mixpanel_service.track("account_deleted", user_id)

    return {"message": "Account and all associated data have been permanently deleted."}
