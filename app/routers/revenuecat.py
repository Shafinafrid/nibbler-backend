"""
RevenueCat → backend subscription sync (July 2026).

Before this existed, NOTHING ever wrote is_premium/premium_until: every user
silently dropped to the free tier when their 7-day trial ended — including
paying subscribers. Two sync paths now exist:

  POST /webhooks/revenuecat  (this file) — RevenueCat's server calls it on
       every purchase/renewal/expiration. Authenticated by a shared secret in
       the Authorization header (configured in the RC dashboard webhook).
  POST /auth/sync-premium    (auth.py)   — the app calls it right after a
       purchase/restore so premium activates without waiting for the webhook.

Both write `premium_until` (the entitlement expiry) rather than the
`is_premium` boolean, so `User.effective_premium` expires access naturally
when a lapsed subscription stops renewing. The boolean stays reserved for
manual comps.
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
# period ends, at which point RevenueCat sends EXPIRATION.


def _ms_to_naive_utc(ms) -> Optional[datetime]:
    if not ms:
        return None
    # Model timestamps are naive UTC (datetime.utcnow() comparisons).
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


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

    user = db.query(User).filter(User.id == app_user_id).first()
    if not user:
        logger.warning("RC webhook %s for unknown user %s", event_type, app_user_id)
        return {"ok": True, "handled": "unknown_user"}

    if event_type in _GRANT_EVENTS:
        expires = _ms_to_naive_utc(event.get("expiration_at_ms"))
        user.premium_until = expires
        db.commit()
        logger.info("RC %s: user %s premium_until=%s", event_type, user.id, expires)
        return {"ok": True, "handled": event_type, "premium_until": str(expires)}

    if event_type == "EXPIRATION":
        # Keep the (past) expiry timestamp instead of nulling it: a set-but-past
        # premium_until is how effective_premium recognises a LAPSED subscriber,
        # who must land on the free tier rather than back in the signup trial.
        user.premium_until = _ms_to_naive_utc(event.get("expiration_at_ms")) or datetime.utcnow()
        db.commit()
        logger.info("RC EXPIRATION: user %s premium ended at %s", user.id, user.premium_until)
        return {"ok": True, "handled": "EXPIRATION"}

    logger.info("RC webhook %s for user %s — no action", event_type, user.id)
    return {"ok": True, "handled": "noop"}
