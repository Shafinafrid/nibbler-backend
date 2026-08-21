import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import requests
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.config import get_settings
from app.database import get_db
from app.middleware.auth import (
    get_current_user, get_current_user_allow_pending_erasure,
    verify_firebase_token, get_or_create_user,
)
from app.models.user import User, EmailAccountHistory, normalize_email
from app.models.library import LibraryItem, CleanupTask, AccountErasure
from app.models.profile import Profile
from app.models.streak import Streak
from app.models.user_data import Note, Highlight, ChatMessage, Completion
from app.schemas.user import UserResponse
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService
from app.services import mixpanel_service, deletion_sheets_service, email_service
from app.services.entitlement_service import resolve_entitlement
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
        # Task 8: parity with the webhook's promotional detection. RC's REST
        # subscriber response has no top-level "store" field on the
        # entitlement object the way webhook events do — a promotional
        # grant is instead identifiable by its product_identifier, which RC
        # generates with an "rc_promo_" prefix (confirmed against RC's own
        # docs, not assumed). A lifetime promotional grant still returns a
        # real expires_date here (RC's REST representation always has one,
        # unlike the webhook payload) — is_lifetime therefore isn't
        # derivable from this endpoint; sync-premium always writes
        # premium_until, and the webhook remains authoritative for the
        # is_premium/lifetime distinction, which it can actually observe.
        is_promotional = str(entitlement.get("product_identifier") or "").startswith("rc_promo_")
        current_user.premium_until = expires
        current_user.entitlement_source = "complimentary" if is_promotional else "paid"
        if not is_promotional:
            current_user.has_held_paid_entitlement = True
        current_user.premium_synced_at = datetime.utcnow()
    # No entitlement in the payload → leave premium_until untouched. RevenueCat
    # keeps expired entitlements in the subscriber object, so "missing" means
    # the user never subscribed — and wiping a stored past expiry would wrongly
    # re-open the signup trial for a lapsed subscriber.

    db.commit()
    db.refresh(current_user)
    logger.info("sync-premium: user %s premium_until=%s", current_user.id, current_user.premium_until)
    return current_user


@router.get("/entitlement")
def get_entitlement(current_user: User = Depends(get_current_user)):
    """Task 8 (Aug 2026): the ONE canonical entitlement result — access,
    source (free/trial/paid/complimentary), start/expiry, whether paid
    access has ever been held, and last-sync time. This is what the mobile
    app must treat as authoritative for tier display/gating; RevenueCat
    stays responsible only for offerings and the purchase/restore
    transaction itself (see resolve_entitlement's docstring)."""
    return resolve_entitlement(current_user)


def _plan_label(user: User) -> str:
    if user.is_premium:
        return "premium (comp)"
    now = datetime.utcnow()
    if user.premium_until and user.premium_until > now:
        return "premium (subscription)"
    if user.premium_until:
        return "free (lapsed subscriber)"
    anchor = user.trial_anchor_at or user.created_at
    if anchor and (now - anchor).days < 7:
        return "trial"
    return "free"


def _revenuecat_subscriber_detail(user_id: str) -> dict:
    """Best-effort READ of the RC subscriber record for the snapshot tab —
    never raises, never blocks erasure-request creation. Separate from
    _delete_revenuecat_subscriber (below), which is the actual erasure
    DELETE call made later during cleanup."""
    if not settings.revenuecat_secret_api_key:
        return {}
    try:
        resp = requests.get(
            f"https://api.revenuecat.com/v1/subscribers/{user_id}",
            headers={"Authorization": f"Bearer {settings.revenuecat_secret_api_key}"},
            timeout=10,
        )
        if resp.status_code == 404:
            return {}
        resp.raise_for_status()
        subscriber = (resp.json() or {}).get("subscriber") or {}
        entitlement = (subscriber.get("entitlements") or {}).get(PRO_ENTITLEMENT) or {}
        product_id = entitlement.get("product_identifier")
        sub = (subscriber.get("subscriptions") or {}).get(product_id, {}) if product_id else {}
        return {
            "rc_product_id": product_id or "",
            "rc_original_purchase_date": sub.get("original_purchase_date", ""),
            "rc_store": sub.get("store", ""),
        }
    except Exception as e:
        logger.warning("RevenueCat snapshot lookup failed for %s: %s", user_id, e)
        return {}


def _capture_erasure_snapshot(db: Session, user: User) -> dict:
    """Task 7 (Aug 2026): a rich, PRIVACY-SAFE (metadata/counts only — no
    note/highlight/chat TEXT content) snapshot of the account right before
    it's gone, captured at the same moment as the erasure-critical identity
    (source_keys etc.) — same immutability rationale: the live rows may
    change or vanish before cleanup finishes, this dict does not."""
    profile = db.query(Profile).filter(Profile.user_id == user.id).first()
    growth_state = (profile.growth_state if profile else None) or {}
    profiles = growth_state.get("profiles") or []
    primary = profiles[0] if profiles else {}

    library_items = db.query(LibraryItem).filter(LibraryItem.user_id == user.id).all()
    library_total_mb = round(sum((i.file_size or 0) for i in library_items) / 1_000_000, 2)
    total_vectors = sum((i.chunk_count or 0) for i in library_items)

    streak = db.query(Streak).filter(Streak.user_id == user.id).first()

    notes_count = db.query(Note).filter(Note.user_id == user.id).count()
    highlights_count = db.query(Highlight).filter(Highlight.user_id == user.id).count()
    chat_messages_count = db.query(ChatMessage).filter(ChatMessage.user_id == user.id).count()
    completions_count = db.query(Completion).filter(Completion.user_id == user.id).count()

    rc = _revenuecat_subscriber_detail(user.id)

    return {
        "email": user.email or "",
        "firebase_uid": user.id,
        "created_at_iso": user.created_at.isoformat() if user.created_at else "",
        "trial_anchor_at_iso": (user.trial_anchor_at or user.created_at).isoformat()
            if (user.trial_anchor_at or user.created_at) else "",
        "successful_sources_total": user.successful_sources_total or 0,
        "plan": _plan_label(user),
        "premium_until_iso": user.premium_until.isoformat() if user.premium_until else "",
        "platform": user.platform or "",
        "app_version": user.app_version or "",
        "device_model": user.device_model or "",
        "os_version": user.os_version or "",
        "timezone": user.timezone or "",
        "locale": user.locale or "",
        "growth_profile_count": len(profiles),
        "primary_life_area": primary.get("lifeArea", "") if isinstance(primary, dict) else "",
        "primary_content_mode": primary.get("contentMode", "") if isinstance(primary, dict) else "",
        "library_item_count": len(library_items),
        "library_total_mb": library_total_mb,
        "total_vectors": total_vectors,
        "current_streak": streak.current_streak if streak else 0,
        "longest_streak": streak.longest_streak if streak else 0,
        "total_bites_read": streak.total_bites_read if streak else 0,
        "notes_count": notes_count,
        "highlights_count": highlights_count,
        "chat_messages_count": chat_messages_count,
        "completions_count": completions_count,
        **rc,
    }


def _delete_revenuecat_subscriber(user_id: str) -> bool:
    """Actual erasure call — DELETE /v1/subscribers/{id}, RevenueCat's own
    GDPR erasure endpoint (permanently deletes the subscriber; confirmed
    sufficient for GDPR per RevenueCat's own docs). Deleting the CUSTOMER
    RECORD does NOT cancel a live Apple/Google subscription — that's why
    the mobile confirmation dialog separately points the user at Apple's
    subscription management. A 404 (already deleted, or never existed —
    e.g. this user never purchased) counts as success: the goal is 'no
    data of theirs left in RevenueCat', and there's none to remove."""
    if not settings.revenuecat_secret_api_key:
        return False
    try:
        resp = requests.delete(
            f"https://api.revenuecat.com/v1/subscribers/{user_id}",
            headers={"Authorization": f"Bearer {settings.revenuecat_secret_api_key}"},
            timeout=10,
        )
        if resp.status_code == 404:
            return True
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("RevenueCat subscriber delete failed for %s: %s", user_id, e)
        return False


def _delete_mixpanel_profile_sync(user_id: str) -> bool:
    try:
        return asyncio.run(mixpanel_service.delete_profile(user_id))
    except Exception as e:
        logger.error("Mixpanel profile delete raised for %s: %s", user_id, e)
        return False


def _send_email_sync(*args, **kwargs) -> bool:
    try:
        return asyncio.run(email_service.send_email(*args, **kwargs))
    except Exception as e:
        logger.error("Erasure alert email failed: %s", e)
        return False


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
    # Start from the PREVIOUS attempt's progress, not empty — every
    # per-artifact-class key below gets unconditionally overwritten this
    # attempt regardless (so stale per-class booleans are harmless), but
    # bookkeeping flags that must survive across attempts (alert_sent) do
    # NOT get re-derived here, so starting from {} would silently reset
    # them every single call and the "only alert once" guard below would
    # never actually suppress anything.
    progress = dict(erasure.progress or {})
    if erasure.deletion_started_at is None:
        erasure.deletion_started_at = datetime.utcnow()

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

    # ── RevenueCat subscriber record ────────────────────────────────────
    # Does NOT cancel a live Apple/Google subscription (RevenueCat can't —
    # only Apple/Google can) — the mobile confirmation dialog separately
    # points the user at Apple's subscription management for that.
    try:
        revenuecat_ok = _delete_revenuecat_subscriber(identity.get("firebase_uid") or user_id)
    except Exception as e:
        revenuecat_ok = False
        logger.error("Erasure: RevenueCat delete raised for %s: %s", user_id, e)
    progress["revenuecat"] = revenuecat_ok

    # ── Mixpanel profile ─────────────────────────────────────────────────
    # Erases the stored PEOPLE-PROFILE properties (name/email/plan/
    # platform). Does not purge historical EVENTS already ingested — see
    # mixpanel_service.delete_profile's docstring for why that's a
    # separate, heavier async API, flagged as a known follow-up.
    try:
        mixpanel_ok = _delete_mixpanel_profile_sync(identity.get("firebase_uid") or user_id)
    except Exception as e:
        mixpanel_ok = False
        logger.error("Erasure: Mixpanel delete raised for %s: %s", user_id, e)
    progress["mixpanel"] = mixpanel_ok

    # ── Firebase identity ────────────────────────────────────────────────
    # Audit finding (Aug 2026): UserNotFoundError must count as success, the
    # same as RevenueCat's 404 and Mixpanel's "no profile" case above.
    # Without this, a real (rare but reachable) sequence — Firebase
    # succeeds on attempt N, a DIFFERENT class transiently fails that same
    # attempt, so all_ok is still False and nothing is deleted yet; attempt
    # N+1 re-calls delete_user on the now-already-gone uid — raised
    # UserNotFoundError forever, permanently stranding the erasure past
    # the retry-alert threshold with no path to ever reach all_ok again.
    firebase_ok = True
    try:
        import firebase_admin.auth as firebase_auth
        try:
            firebase_auth.delete_user(identity.get("firebase_uid") or user_id)
        except firebase_auth.UserNotFoundError:
            pass  # already gone — exactly the outcome we wanted
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
    all_ok = (vectors_ok and s3_files_ok and s3_images_ok and avatar_ok and ledger_ok
              and revenuecat_ok and mixpanel_ok and firebase_ok)
    snapshot = identity.get("snapshot") or {}

    if all_ok:
        # Never remove the final Postgres retry identity until every
        # required remote cleanup is CONFIRMED — this is that moment.
        erasure.state = "resolved"
        erasure.deletion_completed_at = datetime.utcnow()
        erasure.personal_data_redacted_at = datetime.utcnow()

        user = db.query(User).filter(User.id == user_id).first()
        if user:
            # Task 7: durable per-email trial/upload guard, written
            # atomically with the user row's deletion — see
            # EmailAccountHistory's docstring in app/models/user.py.
            email = snapshot.get("email") or user.email
            trial_anchor_iso = snapshot.get("trial_anchor_at_iso")
            trial_anchor = (
                datetime.fromisoformat(trial_anchor_iso) if trial_anchor_iso
                else (user.trial_anchor_at or user.created_at or datetime.utcnow())
            )
            lifetime_total = snapshot.get("successful_sources_total", user.successful_sources_total)
            norm_email = normalize_email(email)
            if norm_email:
                guard = db.query(EmailAccountHistory).filter(EmailAccountHistory.email == norm_email).first()
                if guard:
                    guard.trial_anchor_at = min(guard.trial_anchor_at, trial_anchor)
                    guard.lifetime_successful_sources_total = max(
                        guard.lifetime_successful_sources_total, lifetime_total)
                    guard.deletions_count = (guard.deletions_count or 0) + 1
                else:
                    db.add(EmailAccountHistory(
                        email=norm_email, trial_anchor_at=trial_anchor,
                        lifetime_successful_sources_total=lifetime_total,
                        first_seen_user_id=user.id,
                    ))

        # Audit finding: freeze the Sheet row VALUES and allocate row
        # NUMBERS now (reads current in-memory state; row-number
        # allocation is a Sheets API call, not a Postgres write, so it's
        # safe here) — but do NOT perform the actual network WRITE until
        # after the commit below succeeds. Writing before commit risked
        # the Sheet permanently showing "resolved" for a job whose
        # Postgres commit then failed or rolled back.
        prepared_sheet_write = None
        try:
            prepared_sheet_write = deletion_sheets_service.prepare_success_write(erasure, snapshot)
        except Exception as e:
            logger.warning("Deletion-sheet row allocation failed for %s: %s", user_id, e)

        if user:
            db.delete(user)  # CASCADE handles every FK'd child table
        # Audit finding: the erasure row used to be deleted in this SAME
        # commit, before the Sheet write / confirmation email were even
        # attempted — if either failed afterward, there was no durable
        # record left to retry it from. The actual GDPR-critical action
        # (the user row and everything it cascades to) is genuinely gone
        # the instant this commits; the row below is kept a while longer
        # specifically so the Sheet mirror and the confirmation email —
        # both best-effort, both external calls that can fail — have
        # something durable to retry against. `retry_unsynced_erasure_
        # mirrors` (entitlement_service.py) is what picks a row back up
        # from here; it never re-runs deletion, only the two steps below.
        db.commit()
        logger.info("Erasure complete for user %s", user_id)

        # Postgres is now authoritative and durable — the Sheet write and
        # the confirmation email are best-effort mirrors of an already-true
        # fact, attempted AFTER that fact is durable, never before.
        sheet_ok = deletion_sheets_service.write_prepared_rows(prepared_sheet_write)

        # Completion email — best-effort, uses the captured snapshot email
        # (independent of whether the Firebase record still exists).
        email_sent = False
        if snapshot.get("email"):
            try:
                email_sent = _send_email_sync(
                    to=snapshot["email"],
                    subject="Your Nibbler account has been deleted",
                    html="<p>Your Nibbler account and all associated data have been "
                         "permanently deleted, as requested. This cannot be undone.</p>",
                    text="Your Nibbler account and all associated data have been "
                         "permanently deleted, as requested. This cannot be undone.",
                )
            except Exception as e:
                logger.warning("Deletion confirmation email failed for %s: %s", user_id, e)
        else:
            email_sent = True  # nothing to send — not an outstanding obligation

        erasure_row = db.query(AccountErasure).filter(AccountErasure.id == erasure.id).first()
        if erasure_row:
            progress = dict(erasure_row.progress or {})
            progress["email_sent"] = email_sent
            progress["sheet_synced"] = sheet_ok
            erasure_row.progress = progress
            if sheet_ok and email_sent:
                # Both best-effort obligations are confirmed done — safe to
                # remove the retry identity now (matches the ORIGINAL intent
                # of the comment above this block, just gated on the right
                # things finishing first).
                db.delete(erasure_row)
            db.commit()

        return True

    erasure.state = "failed"
    erasure.retry_count = (erasure.retry_count or 0) + 1

    # Operational alert once retries are clearly exhausted — not resent
    # every 5-minute tick after the first alert.
    ALERT_AFTER_RETRIES = 5
    if erasure.retry_count >= ALERT_AFTER_RETRIES and not progress.get("alert_sent"):
        failing = [k for k, v in progress.items() if not v]
        alert_ok = _send_email_sync(
            to=settings.account_deletion_alert_email,
            subject=f"Nibbler: account erasure stuck ({user_id})",
            html=f"<p>Erasure {erasure.id} for user {user_id} has failed "
                 f"{erasure.retry_count} times. Still failing: {failing}. "
                 f"Manual intervention likely required.</p>",
            text=f"Erasure {erasure.id} for user {user_id} has failed "
                 f"{erasure.retry_count} times. Still failing: {failing}. "
                 f"Manual intervention likely required.",
        )
        progress["alert_sent"] = alert_ok

    erasure.progress = progress
    db.commit()

    try:
        deletion_sheets_service.sync_erasure_to_sheet(erasure, snapshot)
        # Bug found while implementing The grace period's scheduling-log write (Aug
        # 2026): sync_erasure_to_sheet mutates erasure.sheet_row/
        # snapshot_sheet_row IN MEMORY (via _allocate_sheet_rows) but never
        # commits — get_db() only closes the session, it doesn't commit on
        # exit. Without this, the row number never actually persisted, so
        # every retry re-allocated a BRAND NEW sheet row instead of
        # updating the one already assigned — silently defeating the
        # "create-or-update" behavior sync_erasure_to_sheet's own docstring
        # promises. Confirmed by direct reproduction (a fresh session after
        # a mutate-then-close-without-commit sees sheet_row still None).
        db.commit()
    except Exception as e:
        logger.warning("Deletion-sheet sync failed for %s: %s", user_id, e)

    logger.error(
        "ERASURE INCOMPLETE for user %s — %s. Durably tombstoned for retry.",
        user_id, {k: v for k, v in progress.items() if not v},
    )
    return False


def _capture_erasure_identity(db: Session, user: User) -> dict:
    """Builds the durable erasure identity dict for a user — every current
    source-file key, every image key, the avatar key, the Pinecone
    namespace, every still-unresolved cleanup-ledger artifact, the
    Firebase uid, and a privacy-safe snapshot. Frozen at the moment this
    is called, so it MUST be called right before erasure work actually
    begins (grace-period expiry — see entitlement_service.
    promote_scheduled_erasures), not at the moment deletion is first
    requested: Deletion grace period (Aug 2026) made the account stay fully usable for
    the whole grace period, so a new upload or a deleted book in the
    meantime would make an identity captured earlier stale."""
    user_id = user.id
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
    # Task 7 (Aug 2026): rich, privacy-safe (metadata/counts only)
    # snapshot of the account as it stood at THIS moment — same
    # immutability rationale as source_keys/image_keys above. Feeds
    # the "User Snapshot" Sheet tab and the trial/upload abuse guard.
    snapshot = _capture_erasure_snapshot(db, user)

    return {
        "source_keys": source_keys,
        "image_keys": image_keys,
        "avatar_key": user.avatar_url,
        "pinecone_namespace": user_id,
        "cleanup_ledger_ids": cleanup_ledger_ids,
        "firebase_uid": user_id,
        "snapshot": snapshot,
    }


@router.delete("/me")
def delete_account(
    current_user: User = Depends(get_current_user_allow_pending_erasure),
    db: Session = Depends(get_db),
):
    """
    Schedule permanent deletion of the user account and all associated
    data. GDPR Article 17 — Right to Erasure.

    Deletion grace period (Aug 2026): a 24-hour (configurable) grace period, so tapping
    delete on impulse isn't instantly irreversible. This FIRST call only
    creates a 'scheduled' AccountErasure row (no identity captured yet,
    account stays fully usable, nothing gated) and sends a confirmation
    email with a cancel-anytime notice. `POST /auth/cancel-deletion`
    removes it cleanly with zero trace. Nothing external is touched, and
    no Postgres/Firebase/S3/Pinecone cleanup happens, until
    entitlement_service.promote_scheduled_erasures moves it to 'pending'
    once the grace period elapses — from that point on, this is the same
    durable state machine as before this grace period was added (Task 2 closeout, Verified
    Blocker 8): every artifact class attempted regardless of an earlier
    one failing, fail-closed gate applies, retried automatically, and a
    repeated call here is idempotent and reports truthfully.
    """
    user_id = current_user.id
    grace = timedelta(hours=settings.account_deletion_grace_hours)

    erasure = db.query(AccountErasure).filter(AccountErasure.user_id == user_id).first()

    if erasure is None:
        erasure = AccountErasure(id=str(uuid.uuid4()), user_id=user_id, state="scheduled", identity={})
        db.add(erasure)
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            logger.error("Could not schedule account deletion for %s: %s", user_id, e)
            raise HTTPException(
                status_code=503,
                detail="Could not schedule account deletion right now — please try again.",
            )
        db.refresh(erasure)

        scheduled_for = erasure.requested_at + grace
        if current_user.email:
            _send_email_sync(
                to=current_user.email,
                subject="Nibbler account deletion scheduled",
                html=f"<p>Your Nibbler account is scheduled to be permanently deleted on "
                     f"{scheduled_for.strftime('%B %d, %Y at %H:%M UTC')}. Your account stays fully "
                     f"usable until then. Changed your mind? Open the app and go to Profile to cancel "
                     f"anytime before that date — after it passes, deletion cannot be undone.</p>",
                text=f"Your Nibbler account is scheduled to be permanently deleted on "
                     f"{scheduled_for.strftime('%B %d, %Y at %H:%M UTC')}. Your account stays fully "
                     f"usable until then. Changed your mind? Open the app and go to Profile to cancel "
                     f"anytime before that date — after it passes, deletion cannot be undone.",
            )

        # Founder decision (Aug 2026): log the scheduling event itself, not
        # just the eventual completion/failure — a cancelled deletion would
        # otherwise leave literally zero trace anywhere, including in the
        # ops-facing audit trail. This is a LIGHTWEIGHT display-only
        # snapshot (email/plan/device/counts) purely for the Sheet row —
        # NOT the durable cleanup identity, which still only gets captured
        # fresh at promote_scheduled_erasures, per the whole point of the
        # grace period (avoiding a stale target for the actual deletion).
        try:
            display_snapshot = _capture_erasure_snapshot(db, current_user)
            deletion_sheets_service.sync_erasure_to_sheet(erasure, display_snapshot)
            db.commit()  # persists sheet_row/snapshot_sheet_row so later syncs update this SAME row
        except Exception as e:
            logger.warning("Deletion-sheet scheduling sync failed for %s: %s", user_id, e)

        return {
            "message": f"Account scheduled for deletion on {scheduled_for.isoformat()}. "
                       f"You can cancel anytime before then from Profile.",
            "complete": False,
            "scheduled": True,
            "scheduled_for": scheduled_for.isoformat(),
        }

    if erasure.state == "scheduled":
        # Idempotent repeat: report the existing schedule, don't reset the timer.
        scheduled_for = erasure.requested_at + grace
        return {
            "message": f"Account already scheduled for deletion on {scheduled_for.isoformat()}. "
                       f"You can cancel anytime before then from Profile.",
            "complete": False,
            "scheduled": True,
            "scheduled_for": scheduled_for.isoformat(),
        }

    if erasure.state == "resolved":
        # Should be unreachable in practice — a fully resolved erasure
        # deletes its own row in the same commit — but idempotent either way.
        return {"message": "Account and all associated data have been permanently deleted.", "complete": True}

    # state in ("pending", "failed") — the grace period has already elapsed
    # and real cleanup is underway/being retried, exactly as before this grace period was added.
    complete = _attempt_account_erasure_cleanup(db, erasure)

    if complete:
        # Audit finding (Aug 2026): this USED to fire a Mixpanel
        # "account_deleted" track() event here — but that runs moments
        # after mixpanel_service.delete_profile(user_id) (now a required
        # step of completion itself) just erased this exact person's
        # profile, and would write a fresh event permanently tied to
        # their identifier right after the code asserted "no data of
        # theirs left in Mixpanel." Dropped rather than risk contradicting
        # that claim — completion no longer emits any Mixpanel event.
        return {
            "message": "Account and all associated data have been permanently deleted.",
            "complete": True,
        }

    return {
        "message": "Account deletion accepted and still in progress — remaining cleanup is being retried automatically.",
        "complete": False,
    }


@router.post("/cancel-deletion")
def cancel_deletion(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deletion grace period (Aug 2026): cancels a still-'scheduled' deletion — no trace
    left, account carries on exactly as if delete was never tapped. Uses
    the NORMAL get_current_user dependency (not allow_pending_erasure):
    a 'scheduled' row deliberately doesn't trip the fail-closed gate, so
    this is reachable throughout the whole grace period; once the grace
    period elapses and the row becomes 'pending'/'failed', the gate has
    already locked the account out before this could ever be called, and
    there is nothing left to cancel by then anyway.

    Row-locked (matches the codebase's existing lock-order convention,
    e.g. the RevenueCat webhook's user-row lock) against
    promote_scheduled_erasures promoting this SAME row in the scheduler
    thread at the same instant — without the lock and the re-check of
    `state` after acquiring it, a cancel racing the exact moment the
    grace period elapses could delete a row that promotion had just
    populated with a freshly-captured identity and moved to 'pending'.

    Founder decision (Aug 2026): a cancellation is also logged to the
    Sheet audit trail (updates the SAME row scheduling wrote, using the
    saved sheet_row/snapshot_sheet_row — no new row). The row is deleted
    from Postgres and committed FIRST, releasing this lock, BEFORE any
    Sheets network call runs — matching the exact discipline
    promote_scheduled_erasures was fixed to follow (audit finding: never
    hold a Postgres row lock across live network I/O). `_capture_erasure_
    snapshot` alone calls RevenueCat's API, so it must run unlocked too."""
    erasure = (
        db.query(AccountErasure)
        .filter(AccountErasure.user_id == current_user.id)
        .with_for_update()
        .first()
    )
    if erasure is None or erasure.state != "scheduled":
        return {"cancelled": False, "message": "No scheduled deletion to cancel."}

    # Freeze what the Sheet log needs into a plain, DETACHED copy — never
    # added to any session, so it stays safely readable after the real row
    # is deleted and committed below (the real `erasure` ORM object would
    # be expired/gone by then).
    cancelled_copy = AccountErasure(
        id=erasure.id, user_id=erasure.user_id, state="cancelled", identity={},
        sheet_row=erasure.sheet_row, snapshot_sheet_row=erasure.snapshot_sheet_row,
        requested_at=erasure.requested_at,
    )

    db.delete(erasure)
    db.commit()

    try:
        cancel_snapshot = _capture_erasure_snapshot(db, current_user)
        deletion_sheets_service.sync_erasure_to_sheet(cancelled_copy, cancel_snapshot)
    except Exception as e:
        logger.warning("Deletion-sheet cancellation sync failed for %s: %s", current_user.id, e)

    return {"cancelled": True, "message": "Account deletion cancelled."}


@router.get("/deletion-status")
def deletion_status(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deletion grace period (Aug 2026): lets the app show a persistent 'scheduled for
    deletion' banner across app restarts, not just right after the
    DELETE /auth/me response. Only ever reports a 'scheduled' row — once
    promoted to 'pending'/'failed' the fail-closed gate already blocks
    this account everywhere, this endpoint included.

    Audit finding (Aug 2026): grace_period_hours is ALWAYS included, not
    just alongside a real scheduled deletion — the app's pre-confirmation
    dialog (shown BEFORE any DELETE /auth/me call, so it has nothing else
    to read the real number from) fetches this on Profile mount and uses
    it instead of a hardcoded "24 hours" that would silently go stale the
    moment this setting is ever reconfigured."""
    grace = timedelta(hours=settings.account_deletion_grace_hours)
    erasure = (
        db.query(AccountErasure)
        .filter(AccountErasure.user_id == current_user.id, AccountErasure.state == "scheduled")
        .first()
    )
    if erasure is None:
        return {"scheduled": False, "grace_period_hours": settings.account_deletion_grace_hours}
    return {
        "scheduled": True,
        "scheduled_for": (erasure.requested_at + grace).isoformat(),
        "grace_period_hours": settings.account_deletion_grace_hours,
    }
