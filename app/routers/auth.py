import logging
import uuid
from datetime import datetime, timezone

import requests
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_db
from app.middleware.auth import (
    get_current_user, get_current_user_allow_pending_erasure,
    verify_firebase_token, get_or_create_user,
)
from app.models.user import User
from app.models.library import LibraryItem, CleanupTask, AccountErasure
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


def _attempt_account_erasure_cleanup(db: Session, erasure: AccountErasure) -> bool:
    """Task 2 closeout (Verified Blocker 8): attempt every independent
    artifact class this erasure's durable `identity` names, exactly as
    determined at the moment erasure was FIRST requested — never
    re-derived from the (possibly since-changed, possibly since-deleted)
    live rows. Every class is attempted regardless of an earlier one
    failing (per-class try/except), and `erasure.progress` records each
    outcome so a caller/retry can see exactly what still needs doing.

    On full success: removes the remaining local rows (the User row —
    CASCADE handles every child table — and this erasure record) in ONE
    commit, and returns True. On partial failure: persists 'failed' +
    incremented retry_count + the per-class progress, and returns False —
    the row survives, tombstoned, for the next attempt (a user re-calling
    DELETE, or the autonomous scheduler retry).

    Shared by BOTH the synchronous `DELETE /auth/me` call and
    `entitlement_service.retry_account_erasures` — one implementation,
    never two that could drift."""
    identity = erasure.identity or {}
    user_id = erasure.user_id
    progress = {}

    # ── vectors (entire Pinecone namespace) ─────────────────────────────
    try:
        vectors_ok = EmbeddingService().delete_user_namespace(
            identity.get("pinecone_namespace") or user_id)
    except Exception as e:
        vectors_ok = False
        logger.error("Erasure: Pinecone deletion failed for user %s: %s", user_id, e)
    progress["vectors"] = vectors_ok

    # ── source files ─────────────────────────────────────────────────────
    s3_files_ok = True
    try:
        s3 = S3Service()
        for key in identity.get("source_keys") or []:
            try:
                if not s3.delete_file(key):
                    s3_files_ok = False
            except Exception as e:  # noqa: BLE001
                logger.error("Erasure: source-file delete raised for %s (%s): %s", user_id, key, e)
                s3_files_ok = False
    except Exception as e:
        s3_files_ok = False
        logger.error("Erasure: could not build S3 client for source files (%s): %s", user_id, e)
    progress["s3_files"] = s3_files_ok

    # ── extracted book images ───────────────────────────────────────────
    s3_images_ok = True
    try:
        s3 = S3Service()
        for key in identity.get("image_keys") or []:
            try:
                if not s3.delete_file(key):
                    s3_images_ok = False
            except Exception as e:  # noqa: BLE001
                logger.error("Erasure: image delete raised for %s (%s): %s", user_id, key, e)
                s3_images_ok = False
    except Exception as e:
        s3_images_ok = False
        logger.error("Erasure: could not build S3 client for images (%s): %s", user_id, e)
    progress["s3_images"] = s3_images_ok

    # ── avatar ───────────────────────────────────────────────────────────
    avatar_ok = True
    avatar_key = identity.get("avatar_key")
    if avatar_key:
        try:
            avatar_ok = bool(S3Service().delete_file(avatar_key))
        except Exception as e:
            avatar_ok = False
            logger.error("Erasure: avatar delete failed for %s: %s", user_id, e)
    progress["avatar"] = avatar_ok

    # ── any still-unresolved cleanup-ledger artifact this account owns ──
    # (attempt-scoped compensating cleanup from ordinary ingestion —
    # independent of the library items themselves, since a stale attempt's
    # own record can outlive the item it was for).
    ledger_ok = True
    for task_id in identity.get("cleanup_ledger_ids") or []:
        task = db.query(CleanupTask).filter(CleanupTask.id == task_id).first()
        if not task or task.cleanup_state == "resolved":
            continue
        try:
            if task.artifact_kind == "vectors":
                result = EmbeddingService().delete_item_vectors(
                    task.item_id, user_id=task.user_id, attempt_token=task.attempt_token)
            elif task.artifact_kind in ("s3", "s3_image") and task.artifact_key:
                result = S3Service().delete_file(task.artifact_key)
            else:
                result = False
            if result:
                task.cleanup_state = "resolved"
                task.reason = None
            else:
                ledger_ok = False
                task.cleanup_state = "failed"
                task.retry_count = (task.retry_count or 0) + 1
        except Exception as e:
            ledger_ok = False
            logger.error("Erasure: ledger artifact %s cleanup raised for %s: %s", task_id, user_id, e)
    progress["ledger_artifacts"] = ledger_ok

    # ── Firebase identity ────────────────────────────────────────────────
    firebase_ok = True
    try:
        import firebase_admin.auth as firebase_auth
        firebase_auth.delete_user(identity.get("firebase_uid") or user_id)
    except Exception as e:
        firebase_ok = False
        # The durable erasure row (and therefore the fail-closed gate)
        # survives exactly because of failures like this one — a
        # surviving Firebase identity must NOT be able to sign back in
        # and auto-create a fresh, empty account while erasure is
        # pending (see _erasure_gate in app/middleware/auth.py).
        logger.error("Erasure: Firebase account deletion failed for user %s: %s", user_id, e)
    progress["firebase"] = firebase_ok

    erasure.progress = progress
    all_ok = vectors_ok and s3_files_ok and s3_images_ok and avatar_ok and ledger_ok and firebase_ok

    if all_ok:
        # Never remove the final Postgres retry identity until every
        # required remote cleanup is CONFIRMED — this is that moment.
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            db.delete(user)  # CASCADE handles every FK'd child table
        db.delete(erasure)
        db.commit()
        logger.info("Erasure complete for user %s", user_id)
        return True

    erasure.state = "failed"
    erasure.retry_count = (erasure.retry_count or 0) + 1
    db.commit()
    logger.error(
        "ERASURE INCOMPLETE for user %s — %s. Durably tombstoned for retry.",
        user_id, {k: v for k, v in progress.items() if not v},
    )
    return False


@router.delete("/me")
def delete_account(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user_allow_pending_erasure),
    db: Session = Depends(get_db),
):
    """
    Permanently delete the user account and all associated data.
    GDPR Article 17 — Right to Erasure.

    Task 2 closeout (Verified Blocker 8): a durable state machine, not a
    single unconditional pass. The FIRST call captures the complete
    cleanup identity (every current source-file key, every image key,
    the avatar key, the Pinecone namespace, every still-unresolved
    cleanup-ledger artifact, the Firebase uid) into a persisted
    `AccountErasure` row BEFORE any external deletion is attempted — if
    that persistence itself fails, this returns an error and touches
    NOTHING external. From that instant, `get_current_user`'s fail-closed
    gate refuses this account everywhere else, even though the Postgres
    `User` row (and the still-valid Firebase token) survive until cleanup
    actually completes. A repeated call (the user tapping delete again,
    or this exact route being hit again) is idempotent: it re-attempts
    whatever remains, using this SAME durable identity, and reports
    truthfully — 'complete: false' with "still in progress" wording,
    never a false "everything is permanently deleted", unless it
    genuinely is.
    """
    user_id = current_user.id

    erasure = db.query(AccountErasure).filter(AccountErasure.user_id == user_id).first()
    if erasure is None:
        library_items = db.query(LibraryItem).filter(LibraryItem.user_id == user_id).all()
        # Task 2 closeout (Verified Blocker 8): keys are checked against
        # this user's own S3 prefix before being captured into the
        # durable erasure identity — the SAME safety property
        # `_delete_item_images`/`get_book_image` already enforce. Without
        # this, a tombstoned-but-not-yet-hard-deleted item still carrying
        # a tampered or stale out-of-prefix key (its own cleanup never
        # fully succeeded, so `item.images`/`file_url` were never
        # corrected) would hand this account's own erasure the ability
        # to delete a DIFFERENT account's object.
        source_keys = [
            i.file_url for i in library_items
            if i.file_url and str(i.file_url).startswith("%s/" % user_id)
        ]
        image_keys = [
            img.get("key") for i in library_items for img in (i.images or [])
            if isinstance(img, dict) and img.get("key")
            and str(img.get("key")).startswith("book-images/%s/" % user_id)
        ]
        _refused = [
            img.get("key") for i in library_items for img in (i.images or [])
            if isinstance(img, dict) and img.get("key")
            and not str(img.get("key")).startswith("book-images/%s/" % user_id)
        ]
        if _refused:
            logger.error("Erasure identity capture refused %d out-of-scope image key(s) for user %s: %s",
                         len(_refused), user_id, _refused)
        cleanup_ledger_ids = [
            r.id for r in db.query(CleanupTask).filter(
                CleanupTask.user_id == user_id, CleanupTask.cleanup_state != "resolved",
            ).all()
        ]
        identity = {
            "source_keys": source_keys,
            "image_keys": image_keys,
            "avatar_key": current_user.avatar_url,
            "pinecone_namespace": user_id,
            "cleanup_ledger_ids": cleanup_ledger_ids,
            "firebase_uid": user_id,
        }
        erasure = AccountErasure(id=str(uuid.uuid4()), user_id=user_id, state="pending", identity=identity)
        db.add(erasure)
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("Could not persist durable erasure identity for %s: %s", user_id, e)
            raise HTTPException(
                status_code=503,
                detail="Could not start account deletion right now — please try again.",
            )
        db.refresh(erasure)
    elif erasure.state == "resolved":
        # Should be unreachable in practice — a fully resolved erasure
        # deletes its own row in the same commit — but idempotent either way.
        return {"message": "Account and all associated data have been permanently deleted.", "complete": True}

    complete = _attempt_account_erasure_cleanup(db, erasure)

    if complete:
        background_tasks.add_task(mixpanel_service.track, "account_deleted", user_id)
        return {
            "message": "Account and all associated data have been permanently deleted.",
            "complete": True,
        }

    return {
        "message": "Account deletion accepted and still in progress — remaining cleanup is being retried automatically.",
        "complete": False,
    }
