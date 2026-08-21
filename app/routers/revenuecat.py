"""
RevenueCat → backend subscription sync (July 2026; extended for Task 8, Aug 2026).

Before this existed, NOTHING ever wrote is_premium/premium_until: every user
silently dropped to the free tier when their 7-day trial ended — including
paying subscribers. Two sync paths exist:

  POST /webhooks/revenuecat  (this file) — RevenueCat's server calls it on
       every purchase/renewal/expiration/promotional grant. Authenticated by
       a shared secret in the Authorization header (configured in the RC
       dashboard webhook).
  POST /auth/sync-premium    (auth.py)   — the app calls it right after a
       purchase/restore so premium activates without waiting for the webhook.

Task 8 (Aug 2026) additions, grounded against RevenueCat's own docs rather
than assumed (see event-types-and-fields + the community's confirmation that
revoking a promotional entitlement sets its expiration to the revocation
moment rather than deleting it):

  - Grant events now inspect `event["store"]`. `"PROMOTIONAL"` means this
    came from the RC dashboard (a founder/tester comp), not a real purchase —
    written as `entitlement_source="complimentary"`, never touching
    `has_held_paid_entitlement`. Every other store value is a real purchase —
    `entitlement_source="paid"`, `has_held_paid_entitlement` permanently True.
  - A promotional grant with NO `expiration_at_ms` is RC's "Lifetime"
    duration — modelled as `is_premium=True` (permanent, matches the
    pre-existing "is_premium reserved for comps" convention) rather than a
    premium_until far in the future. A time-limited promotional grant uses
    `premium_until` exactly like a real subscription does, so it expires
    naturally through the SAME `effective_premium`/EXPIRATION path — no
    separate "check if the comp has run out" job is needed anywhere.
  - EXPIRATION clears `is_premium`/`entitlement_source` ONLY when this
    expiration is actually about the promotional grant (event store is
    PROMOTIONAL, or — defensively — the account's current entitlement_source
    already says 'complimentary') — this is what revokes a LIFETIME
    promotional grant when an admin removes it from the dashboard (RC fires
    EXPIRATION for that, per its own docs, exactly as it does for a real
    subscription's natural end). An account CAN hold a comp and a real
    subscription at once (the grant branch never touches is_premium for a
    real purchase); an unconditional clear here would silently revoke a
    still-valid comp whenever an UNRELATED real subscription lapses — audit
    finding, Aug 2026.
  - Idempotency: RC's own docs state retries reuse the same `id` and
    `event_timestamp_ms`. `last_webhook_event_id` catches an exact duplicate
    redelivery; `last_webhook_event_at_ms` catches a genuinely-older event
    arriving out of order after a newer one was already applied (e.g. a
    delayed RENEWAL landing after a subsequent CANCELLATION+EXPIRATION pair
    already ran) — without this a stale event could silently resurrect
    access that was correctly already revoked.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.models.user import User

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Event types that carry a fresh entitlement expiry
_GRANT_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "PRODUCT_CHANGE",
    "NON_RENEWING_PURCHASE",
}
# CANCELLATION is deliberately ignored: the user keeps access until the paid
# period ends, at which point RevenueCat sends EXPIRATION. Promotional
# entitlements aren't end-user-cancellable (no store-level auto-renew toggle)
# so this event type never applies to a comp — it's exclusively the real-
# subscription "user turned off auto-renew" signal.

PROMOTIONAL_STORE = "PROMOTIONAL"


def _ms_to_naive_utc(ms) -> Optional[datetime]:
    if not ms:
        return None
    # Model timestamps are naive UTC (datetime.utcnow() comparisons).
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def _is_stale_or_duplicate(user: User, event: dict) -> bool:
    """True if this exact event was already applied, or a strictly newer
    event has already been applied since. Never raises — a malformed/missing
    id or timestamp is treated as 'not verifiably stale' (apply it), since
    refusing a legitimate event because RC omitted a field would be worse
    than the rare double-apply that field would have prevented."""
    event_id = event.get("id")
    event_ms = event.get("event_timestamp_ms")
    if event_id and user.last_webhook_event_id == event_id:
        return True
    if event_ms and user.last_webhook_event_at_ms and event_ms < user.last_webhook_event_at_ms:
        return True
    return False


def _stamp_processed(user: User, event: dict) -> None:
    event_id = event.get("id")
    event_ms = event.get("event_timestamp_ms")
    if event_id:
        user.last_webhook_event_id = event_id
    if event_ms:
        user.last_webhook_event_at_ms = event_ms


@router.post("/revenuecat")
def revenuecat_webhook(
    payload: dict,
    authorization: str = Header(default=""),
    db: Session = Depends(get_db),
):
    if not settings.revenuecat_webhook_secret:
        # Refuse to process unauthenticated events rather than trusting anyone.
        raise HTTPException(status_code=503, detail="Webhook secret not configured.")
    if authorization != settings.revenuecat_webhook_secret:
        raise HTTPException(status_code=401, detail="Bad webhook authorization.")

    event = payload.get("event") or {}
    event_type = event.get("type", "")
    # The app calls Purchases.logIn(firebaseUid), so app_user_id == users.id.
    app_user_id = event.get("app_user_id") or event.get("original_app_user_id") or ""

    if event_type == "TEST":
        return {"ok": True, "handled": "TEST"}

    if not app_user_id or app_user_id.startswith("$RCAnonymousID"):
        # Nothing to map — 200 so RevenueCat doesn't retry forever.
        logger.warning("RC webhook %s for unmappable app_user_id=%r", event_type, app_user_id)
        return {"ok": True, "handled": "ignored_anonymous"}

    # Audit finding (Aug 2026): row-locked, matching this codebase's own
    # documented lock-order convention (see entitlement_service.py's "GLOBAL
    # POSTGRESQL LOCK ORDER" section). This route is a sync `def`, so
    # FastAPI runs it in the threadpool — two webhook deliveries for the
    # SAME user CAN genuinely execute concurrently. Without this lock, both
    # could read the row before either commits, both pass the staleness
    # check below, and whichever commits LAST wins regardless of which
    # event was actually newer — an older RENEWAL landing after a newer
    # EXPIRATION could silently resurrect access, and (worse) regress
    # last_webhook_event_at_ms backward, permanently breaking the ordering
    # guard for every event after it.
    user = db.query(User).filter(User.id == app_user_id).with_for_update().first()
    if not user:
        logger.warning("RC webhook %s for unknown user %s", event_type, app_user_id)
        return {"ok": True, "handled": "unknown_user"}

    if event_type not in _GRANT_EVENTS and event_type != "EXPIRATION":
        logger.info("RC webhook %s for user %s — no action", event_type, user.id)
        return {"ok": True, "handled": "noop"}

    if _is_stale_or_duplicate(user, event):
        logger.info("RC webhook %s for user %s ignored (duplicate/stale event id=%s ts=%s)",
                    event_type, user.id, event.get("id"), event.get("event_timestamp_ms"))
        return {"ok": True, "handled": "stale_ignored"}

    if event_type in _GRANT_EVENTS:
        is_promotional = event.get("store") == PROMOTIONAL_STORE
        expires = _ms_to_naive_utc(event.get("expiration_at_ms"))

        if is_promotional:
            user.entitlement_source = "complimentary"
            if expires is None:
                # RC's "Lifetime" duration carries no expiration_at_ms at
                # all — this IS the permanent-comp case, not a missing field.
                user.is_premium = True
                user.complimentary_until = None
            else:
                user.is_premium = False
                user.complimentary_until = expires
        else:
            user.entitlement_source = "paid"
            user.has_held_paid_entitlement = True
            user.paid_premium_until = expires
            # Never touch is_premium here — it stays reserved for comps;
            # a real subscription's access lives entirely in paid_premium_until.

        user.recompute_premium_until()
        user.premium_synced_at = datetime.utcnow()
        _stamp_processed(user, event)
        db.commit()
        logger.info("RC %s: user %s source=%s premium_until=%s is_premium=%s",
                    event_type, user.id, user.entitlement_source, expires, user.is_premium)
        return {"ok": True, "handled": event_type, "premium_until": str(expires)}

    # EXPIRATION — covers a real subscription's natural end AND a
    # promotional grant's revocation/natural end (RC sends the same event
    # type for both). Keep the (past) expiry timestamp instead of nulling
    # it: a set-but-past premium_until is how effective_premium recognises
    # a LAPSED subscriber, who must land on the free tier rather than back
    # in the signup trial.
    #
    # Audit finding (Aug 2026, fixed): an account CAN hold two independently-
    # sourced grants at once — e.g. a beta tester given a lifetime comp who
    # later, separately, becomes a real paying subscriber (the grant branch
    # above deliberately never touches is_premium for a real purchase, so
    # both signals coexist on the same row). The OLD code wrote the single
    # shared premium_until UNCONDITIONALLY here, before the source check
    # below even ran — so an unrelated source's expiration silently
    # clobbered the other's still-valid expiry (or falsely resurrected a
    # correctly-lapsed one via the `or datetime.utcnow()` fallback). Each
    # source's expiry now lands ONLY in that source's own column; only
    # clear/expire the signal when THIS expiration is actually about that
    # grant — either the event itself says so, or (as a defensive fallback,
    # in case store is ever absent on an EXPIRATION payload) the account's
    # currently-recorded active source already names it.
    expiry_ts = _ms_to_naive_utc(event.get("expiration_at_ms")) or datetime.utcnow()
    ends_the_comp = event.get("store") == PROMOTIONAL_STORE or user.entitlement_source == "complimentary"
    if ends_the_comp:
        user.is_premium = False  # clears a revoked LIFETIME promotional grant too
        user.complimentary_until = expiry_ts
        user.entitlement_source = None
    elif user.entitlement_source == "paid":
        # The real subscription that just lapsed IS what entitlement_source
        # currently names — clear it, but leave is_premium untouched (it
        # was never this event's concern to begin with).
        user.paid_premium_until = expiry_ts
        user.entitlement_source = None
    else:
        # entitlement_source already reflects neither known source (e.g. a
        # stale/already-cleared row) — best-effort record against whichever
        # column the event's own store says, so the expiry isn't dropped
        # silently. Falls back to paid, matching this branch's pre-existing
        # "not promotional" assumption.
        if event.get("store") == PROMOTIONAL_STORE:
            user.complimentary_until = expiry_ts
        else:
            user.paid_premium_until = expiry_ts

    user.recompute_premium_until()
    user.premium_synced_at = datetime.utcnow()
    _stamp_processed(user, event)
    db.commit()
    logger.info("RC EXPIRATION: user %s premium ended at %s (comp_cleared=%s)",
                user.id, user.premium_until, ends_the_comp)
    return {"ok": True, "handled": "EXPIRATION"}
