import logging
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from app.config import get_settings
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
settings = get_settings()

router = APIRouter(prefix="/auth", tags=["auth"])
security = HTTPBearer()

# Must match the entitlement identifier in the RevenueCat dashboard and
# nibbler/src/services/revenueCat.js
PRO_ENTITLEMENT = "Nibbler Pro"


@router.post("/verify", response_model=UserResponse)
def verify_and_login(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
):
    """Verify Firebase token and return/create user. Called when the app starts or after sign-in."""
    decoded = verify_firebase_token(credentials.credentials)
    user = get_or_create_user(decoded, db)
    return user


@router.get("/me", response_model=UserResponse)
def get_me(current_user: User = Depends(get_current_user)):
    """Return the current authenticated user."""
    return current_user


@router.post("/sync-premium", response_model=UserResponse)
def sync_premium(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Re-check this user's subscription directly with RevenueCat and store the
    entitlement expiry. The app calls this right after a purchase or restore
    so premium activates immediately (the webhook covers renewals/expirations).

    Takes no body on purpose: the server never trusts client-claimed premium
    state — it asks RevenueCat itself.
    """
    if not settings.revenuecat_secret_api_key:
        raise HTTPException(status_code=503, detail="Subscription sync is not configured.")

    try:
        resp = requests.get(
            f"https://api.revenuecat.com/v1/subscribers/{current_user.id}",
            headers={"Authorization": f"Bearer {settings.revenuecat_secret_api_key}"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error("RevenueCat subscriber lookup failed for %s: %s", current_user.id, e)
        raise HTTPException(status_code=502, detail="Could not verify subscription with RevenueCat.")

    entitlement = ((data.get("subscriber") or {}).get("entitlements") or {}).get(PRO_ENTITLEMENT) or {}
    expires_iso = entitlement.get("expires_date")
    if expires_iso:
        expires = (
            datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
            .astimezone(timezone.utc)
            .replace(tzinfo=None)  # model timestamps are naive UTC
        )
        current_user.premium_until = expires
    # No entitlement in the payload → leave premium_until untouched. RevenueCat
    # keeps expired entitlements in the subscriber object, so "missing" means
    # the user never subscribed — and wiping a stored past expiry would wrongly
    # re-open the signup trial for a lapsed subscriber.

    db.commit()
    db.refresh(current_user)
    logger.info("sync-premium: user %s premium_until=%s", current_user.id, current_user.premium_until)
    return current_user


@router.delete("/me")
def delete_account(
    background_tasks: BackgroundTasks,
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
            s3.delete_file(item.file_url)
        logger.info("Deleted %d S3 files for user %s", len(library_items), user_id)
    except Exception as e:
        logger.error("S3 deletion failed for user %s: %s", user_id, e)
        # Continue — partial failure should not block account deletion

    # ── 2. Delete Pinecone vectors ────────────────────────────────────────────
    try:
        embeddings = EmbeddingService()
        embeddings.delete_user_namespace(user_id)
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

    # ── 5. Track analytics (async task — runs on the loop after the response) ─
    background_tasks.add_task(mixpanel_service.track, "account_deleted", user_id)

    return {"message": "Account and all associated data have been permanently deleted."}
