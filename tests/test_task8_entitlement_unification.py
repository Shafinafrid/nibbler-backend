"""
Task 8 (Aug 2026) — canonical entitlement resolver + RevenueCat promotional
entitlements for complimentary access.

  A — resolve_entitlement(): correct access/source/dates for free, trial,
      paid, complimentary (time-limited + lifetime), and lapsed-subscriber.
  B — Webhook: promotional grants (store=PROMOTIONAL) write
      entitlement_source='complimentary', never touch has_held_paid_entitlement;
      real purchases write entitlement_source='paid' + set
      has_held_paid_entitlement permanently.
  C — Webhook: a promotional grant with no expiration_at_ms is a LIFETIME
      grant (is_premium=True), one WITH an expiration_at_ms is time-limited
      (premium_until only).
  D — Webhook: EXPIRATION clears is_premium/entitlement_source — this is
      what actually revokes a promotional grant (including a lifetime one).
  E — Webhook idempotency: an exact-duplicate event (same id) is a no-op;
      a genuinely-older event (earlier event_timestamp_ms) arriving after a
      newer one was already applied does NOT resurrect stale state.
  F — Ordinary CANCELLATION is still ignored (access continues); real
      distinction from promotional revocation is preserved.
  G — sync-premium: promotional detection via the `rc_promo_` product-id
      prefix reaches the same entitlement_source/has_held_paid_entitlement
      outcome as the webhook, from the REST read path.
  H — has_held_paid_entitlement survives a promotional grant/revocation
      cycle once truly set (never cleared by anything but never being paid).

Network stays hermetically blank throughout — the webhook/sync-premium tests
call the route functions directly with synthetic payloads / mocked requests.
"""
import os
import sys
import tempfile
import datetime
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task8.db", FIREBASE_PROJECT_ID="t",
                   REVENUECAT_WEBHOOK_SECRET="test-webhook-secret")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports; re-blanks REVENUECAT_WEBHOOK_SECRET

failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def section(t):
    print(f"\n=== {t} ===")


from app.database import create_tables, SessionLocal
from app.models.user import User
from app.services.entitlement_service import resolve_entitlement
from app.routers.revenuecat import revenuecat_webhook
from app.routers import auth as auth_router
from app.config import get_settings

create_tables()
db = SessionLocal()
settings = get_settings()
# hermetic.py force-blanks REVENUECAT_WEBHOOK_SECRET at import time (it's in
# the module's own _FORCED_BLANK list) — set it back AFTER, on the already-
# constructed settings singleton, since this test specifically needs a real
# configured secret to exercise the webhook's auth path.
settings.revenuecat_webhook_secret = "test-webhook-secret"

NOW = datetime.datetime.utcnow()


def mkuser(uid, **kw):
    kw.setdefault("email", f"{uid}@example.com")
    kw.setdefault("created_at", NOW)
    u = User(id=uid, **kw)
    db.add(u)
    db.commit()
    return u


def webhook(event: dict):
    """Call the route function directly, matching FastAPI's own calling
    convention for a plain callable — no HTTP layer needed."""
    return revenuecat_webhook(
        payload={"event": event}, authorization="test-webhook-secret", db=db)


def refresh(user):
    db.expire_all()
    return db.query(User).filter(User.id == user.id).first()


# ═══════════════════════════════════════════════════════════════════════
section("A — resolve_entitlement()")
# ═══════════════════════════════════════════════════════════════════════

u_free = mkuser("ent_free", created_at=NOW - datetime.timedelta(days=30))
r = resolve_entitlement(u_free)
check("free user: access=free, source=free, no dates", r["access"] == "free" and r["source"] == "free"
      and r["expires_at"] is None and r["starts_at"] is None, r)

u_trial = mkuser("ent_trial", created_at=NOW - datetime.timedelta(days=1))
r = resolve_entitlement(u_trial)
check("trial user: access=premium, source=trial, has a starts/expires window",
      r["access"] == "premium" and r["source"] == "trial" and r["starts_at"] and r["expires_at"], r)

u_paid = mkuser("ent_paid", created_at=NOW - datetime.timedelta(days=30),
                 premium_until=NOW + datetime.timedelta(days=20), entitlement_source="paid",
                 has_held_paid_entitlement=True)
r = resolve_entitlement(u_paid)
check("paid user: access=premium, source=paid, expires_at set, has_held_paid=True",
      r["access"] == "premium" and r["source"] == "paid" and r["expires_at"] is not None
      and r["has_held_paid_entitlement"] is True, r)

u_lapsed = mkuser("ent_lapsed", created_at=NOW - datetime.timedelta(days=30),
                   premium_until=NOW - datetime.timedelta(days=5), entitlement_source="paid",
                   has_held_paid_entitlement=True)
r = resolve_entitlement(u_lapsed)
check("lapsed subscriber: access=free (never falls back to trial), but has_held_paid stays True",
      r["access"] == "free" and r["source"] == "free" and r["has_held_paid_entitlement"] is True, r)

u_comp_timed = mkuser("ent_comp_timed", created_at=NOW - datetime.timedelta(days=30),
                       premium_until=NOW + datetime.timedelta(days=10), entitlement_source="complimentary")
r = resolve_entitlement(u_comp_timed)
check("time-limited comp: access=premium, source=complimentary, expires_at set, NOT lifetime",
      r["access"] == "premium" and r["source"] == "complimentary" and r["expires_at"] is not None
      and r["is_lifetime"] is False, r)

u_comp_lifetime = mkuser("ent_comp_lifetime", created_at=NOW - datetime.timedelta(days=30),
                          is_premium=True, entitlement_source="complimentary")
r = resolve_entitlement(u_comp_lifetime)
check("lifetime comp: access=premium, source=complimentary, no expiry, is_lifetime=True",
      r["access"] == "premium" and r["source"] == "complimentary" and r["expires_at"] is None
      and r["is_lifetime"] is True, r)

# Audit finding: a PRE-Task-8 manual comp — is_premium=True set by hand
# (the only way it could ever have gotten there before this task, since
# nothing ever wrote it in code), with entitlement_source still None
# (either the one-time backfill hasn't reached this row yet, or this
# fixture deliberately bypasses it to test the resolver's own safety net
# directly, independent of whether the migration ran).
u_legacy_comp = mkuser("ent_legacy_comp", created_at=NOW - datetime.timedelta(days=30), is_premium=True)
r = resolve_entitlement(u_legacy_comp)
check("audit fix: a legacy is_premium=True row with NO entitlement_source reports "
      "source='complimentary', NOT 'paid' — this is exactly the founder's own account shape",
      r["source"] == "complimentary" and r["is_lifetime"] is True, r)


# ═══════════════════════════════════════════════════════════════════════
section("B — webhook: promotional vs real purchase classification")
# ═══════════════════════════════════════════════════════════════════════

u_b1 = mkuser("wh_b1")
webhook({"id": "evt-b1", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_b1",
         "store": "PROMOTIONAL", "expiration_at_ms": int((NOW + datetime.timedelta(days=30)).timestamp() * 1000),
         "event_timestamp_ms": 1000})
u_b1 = refresh(u_b1)
check("promotional grant sets entitlement_source='complimentary'", u_b1.entitlement_source == "complimentary")
check("promotional grant does NOT set has_held_paid_entitlement", u_b1.has_held_paid_entitlement is False)
check("promotional grant sets premium_synced_at", u_b1.premium_synced_at is not None)

u_b2 = mkuser("wh_b2")
webhook({"id": "evt-b2", "type": "INITIAL_PURCHASE", "app_user_id": "wh_b2",
         "store": "APP_STORE", "expiration_at_ms": int((NOW + datetime.timedelta(days=30)).timestamp() * 1000),
         "event_timestamp_ms": 1000})
u_b2 = refresh(u_b2)
check("real purchase sets entitlement_source='paid'", u_b2.entitlement_source == "paid")
check("real purchase permanently sets has_held_paid_entitlement", u_b2.has_held_paid_entitlement is True)


# ═══════════════════════════════════════════════════════════════════════
section("C — webhook: lifetime vs time-limited promotional grant")
# ═══════════════════════════════════════════════════════════════════════

u_c1 = mkuser("wh_c1")
webhook({"id": "evt-c1", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_c1",
         "store": "PROMOTIONAL", "event_timestamp_ms": 1000})  # no expiration_at_ms = Lifetime
u_c1 = refresh(u_c1)
check("promotional grant with NO expiration_at_ms is modelled as a LIFETIME grant (is_premium=True)",
      u_c1.is_premium is True and u_c1.premium_until is None, (u_c1.is_premium, u_c1.premium_until))

u_c2 = mkuser("wh_c2")
webhook({"id": "evt-c2", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_c2",
         "store": "PROMOTIONAL", "expiration_at_ms": int((NOW + datetime.timedelta(days=14)).timestamp() * 1000),
         "event_timestamp_ms": 1000})
u_c2 = refresh(u_c2)
check("promotional grant WITH expiration_at_ms is time-limited (is_premium stays False, premium_until set)",
      u_c2.is_premium is False and u_c2.premium_until is not None, (u_c2.is_premium, u_c2.premium_until))


# ═══════════════════════════════════════════════════════════════════════
section("D — webhook: EXPIRATION revokes a promotional grant, including a lifetime one")
# ═══════════════════════════════════════════════════════════════════════

u_d1 = mkuser("wh_d1", is_premium=True, entitlement_source="complimentary")
webhook({"id": "evt-d1", "type": "EXPIRATION", "app_user_id": "wh_d1",
         "expiration_at_ms": int(NOW.timestamp() * 1000), "event_timestamp_ms": 2000})
u_d1 = refresh(u_d1)
check("EXPIRATION clears is_premium (revokes a lifetime comp)", u_d1.is_premium is False)
check("EXPIRATION clears entitlement_source", u_d1.entitlement_source is None)
check("EXPIRATION lands the user on FREE via effective_premium (no trial resurrection)",
      resolve_entitlement(u_d1)["access"] == "free")


# ═══════════════════════════════════════════════════════════════════════
section("E — webhook idempotency: duplicate + out-of-order rejection")
# ═══════════════════════════════════════════════════════════════════════

u_e1 = mkuser("wh_e1")
grant_event = {"id": "evt-e1-dup", "type": "INITIAL_PURCHASE", "app_user_id": "wh_e1",
               "store": "APP_STORE", "expiration_at_ms": int((NOW + datetime.timedelta(days=30)).timestamp() * 1000),
               "event_timestamp_ms": 5000}
r1 = webhook(grant_event)
r2 = webhook(grant_event)  # exact redelivery — RC's own docs: retries reuse the same id
check("first delivery is handled normally", r1["handled"] == "INITIAL_PURCHASE")
check("exact-duplicate redelivery (same id) is recognized as stale/ignored", r2["handled"] == "stale_ignored")

u_e2 = mkuser("wh_e2")
newer_expiration = webhook({"id": "evt-e2-newer", "type": "EXPIRATION", "app_user_id": "wh_e2",
                             "expiration_at_ms": int(NOW.timestamp() * 1000), "event_timestamp_ms": 9000})
older_renewal = webhook({"id": "evt-e2-older", "type": "RENEWAL", "app_user_id": "wh_e2",
                          "store": "APP_STORE",
                          "expiration_at_ms": int((NOW + datetime.timedelta(days=30)).timestamp() * 1000),
                          "event_timestamp_ms": 3000})  # OLDER timestamp, arrives AFTER
u_e2 = refresh(u_e2)
check("a genuinely older event (earlier event_timestamp_ms) arriving after a newer one is ignored",
      older_renewal["handled"] == "stale_ignored")
check("the account correctly STAYS revoked — a stale RENEWAL cannot resurrect access "
      "that was already correctly expired", resolve_entitlement(u_e2)["access"] == "free",
      (u_e2.premium_until, u_e2.is_premium))


# ═══════════════════════════════════════════════════════════════════════
section("F — ordinary CANCELLATION still ignored (retains access)")
# ═══════════════════════════════════════════════════════════════════════

u_f1 = mkuser("wh_f1", premium_until=NOW + datetime.timedelta(days=10), entitlement_source="paid",
              has_held_paid_entitlement=True)
r = webhook({"id": "evt-f1", "type": "CANCELLATION", "app_user_id": "wh_f1", "event_timestamp_ms": 1000})
u_f1 = refresh(u_f1)
check("CANCELLATION is a no-op — access continues until the real EXPIRATION arrives",
      r["handled"] == "noop" and resolve_entitlement(u_f1)["access"] == "premium")


# ═══════════════════════════════════════════════════════════════════════
section("F2 — audit fix: EXPIRATION does NOT wipe an unrelated, still-active comp "
        "(an account can hold a lifetime comp AND a real subscription independently)")
# ═══════════════════════════════════════════════════════════════════════

u_f2 = mkuser("wh_f2", is_premium=True, entitlement_source="paid",
              premium_until=NOW + datetime.timedelta(days=10), has_held_paid_entitlement=True)
# ^ is_premium=True from an EARLIER lifetime comp grant; entitlement_source
# was then overwritten to 'paid' by a LATER real-subscription grant, exactly
# as the grant branch's own "never touch is_premium for a real purchase"
# comment describes — both signals genuinely coexist on this one row.
webhook({"id": "evt-f2", "type": "EXPIRATION", "app_user_id": "wh_f2", "store": "APP_STORE",
         "expiration_at_ms": int(NOW.timestamp() * 1000), "event_timestamp_ms": 1000})
u_f2 = refresh(u_f2)
check("the REAL subscription's expiration clears entitlement_source (no longer 'paid')",
      u_f2.entitlement_source is None, u_f2.entitlement_source)
check("but the unrelated lifetime comp (is_premium) SURVIVES — a real subscription "
      "lapsing must not silently revoke a separately-granted comp",
      u_f2.is_premium is True, u_f2.is_premium)
check("access correctly remains premium (via the surviving comp), not free",
      resolve_entitlement(u_f2)["access"] == "premium")

# The mirror case: the event's own store IS promotional -> the comp itself
# ends, even if entitlement_source currently reads 'paid' for some reason
# (defensive: the event's own signal takes priority when present).
u_f3 = mkuser("wh_f3", is_premium=True, entitlement_source="paid", has_held_paid_entitlement=True)
webhook({"id": "evt-f3", "type": "EXPIRATION", "app_user_id": "wh_f3", "store": "PROMOTIONAL",
         "expiration_at_ms": int(NOW.timestamp() * 1000), "event_timestamp_ms": 1000})
u_f3 = refresh(u_f3)
check("an EXPIRATION whose OWN event store is PROMOTIONAL clears is_premium regardless "
      "of what entitlement_source currently says (event's own signal takes priority)",
      u_f3.is_premium is False and u_f3.entitlement_source is None)


# ═══════════════════════════════════════════════════════════════════════
section("F3 — audit fix: webhook row-locks the user before the staleness check")
# ═══════════════════════════════════════════════════════════════════════

import inspect
webhook_src = inspect.getsource(revenuecat_webhook)
check("the user row is fetched with .with_for_update() — real concurrency protection, "
      "not just a documentation promise (a genuine two-thread race is impractical to "
      "reproduce deterministically against SQLite in this harness; PostgreSQL production "
      "behavior for this exact lock pattern is already proven elsewhere in this codebase, "
      "see entitlement_service.py's GLOBAL POSTGRESQL LOCK ORDER section)",
      ".with_for_update()" in webhook_src and "db.query(User).filter(User.id == app_user_id).with_for_update()" in webhook_src)


# ═══════════════════════════════════════════════════════════════════════
section("G — sync-premium: rc_promo_ prefix detection parity with the webhook")
# ═══════════════════════════════════════════════════════════════════════

u_g1 = mkuser("sync_g1")
with mock.patch("app.routers.auth.requests") as MockRequests, \
     mock.patch("app.routers.auth.settings") as MockSettings:
    MockSettings.revenuecat_secret_api_key = "sk_test"
    MockRequests.get.return_value.raise_for_status = lambda: None
    MockRequests.get.return_value.json.return_value = {
        "subscriber": {"entitlements": {"Nibbler Pro": {
            "expires_date": (NOW + datetime.timedelta(days=30)).isoformat() + "Z",
            "product_identifier": "rc_promo_Nibbler Pro_custom",
        }}}
    }
    auth_router.sync_premium(current_user=u_g1, db=db)
u_g1 = refresh(u_g1)
check("sync-premium recognizes an rc_promo_-prefixed product_identifier as complimentary",
      u_g1.entitlement_source == "complimentary", u_g1.entitlement_source)
check("sync-premium does NOT mark a promotional grant as has_held_paid_entitlement",
      u_g1.has_held_paid_entitlement is False)

u_g2 = mkuser("sync_g2")
with mock.patch("app.routers.auth.requests") as MockRequests2, \
     mock.patch("app.routers.auth.settings") as MockSettings2:
    MockSettings2.revenuecat_secret_api_key = "sk_test"
    MockRequests2.get.return_value.raise_for_status = lambda: None
    MockRequests2.get.return_value.json.return_value = {
        "subscriber": {"entitlements": {"Nibbler Pro": {
            "expires_date": (NOW + datetime.timedelta(days=30)).isoformat() + "Z",
            "product_identifier": "nibbler_pro_annual",
        }}}
    }
    auth_router.sync_premium(current_user=u_g2, db=db)
u_g2 = refresh(u_g2)
check("sync-premium recognizes a real product id as paid", u_g2.entitlement_source == "paid")
check("sync-premium sets has_held_paid_entitlement for a real purchase", u_g2.has_held_paid_entitlement is True)


# ═══════════════════════════════════════════════════════════════════════
section("H — has_held_paid_entitlement is permanent across a comp cycle")
# ═══════════════════════════════════════════════════════════════════════

u_h1 = mkuser("wh_h1")
webhook({"id": "evt-h1a", "type": "INITIAL_PURCHASE", "app_user_id": "wh_h1", "store": "APP_STORE",
         "expiration_at_ms": int((NOW + datetime.timedelta(days=30)).timestamp() * 1000), "event_timestamp_ms": 1000})
u_h1 = refresh(u_h1)
check("setup: has_held_paid_entitlement is True after a real purchase", u_h1.has_held_paid_entitlement is True)

webhook({"id": "evt-h1b", "type": "EXPIRATION", "app_user_id": "wh_h1",
         "expiration_at_ms": int(NOW.timestamp() * 1000), "event_timestamp_ms": 2000})
webhook({"id": "evt-h1c", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_h1", "store": "PROMOTIONAL",
         "event_timestamp_ms": 3000})  # a later complimentary lifetime grant, e.g. as a beta thank-you
u_h1 = refresh(u_h1)
check("has_held_paid_entitlement survives a later complimentary grant (never cleared)",
      u_h1.has_held_paid_entitlement is True)
check("but the CURRENT active source is correctly complimentary, not paid",
      u_h1.entitlement_source == "complimentary")


# ═══════════════════════════════════════════════════════════════════════
section("I — audit fix: the one-time backfill actually populates entitlement_source "
        "for pre-existing legacy is_premium=True rows")
# ═══════════════════════════════════════════════════════════════════════

u_backfill = mkuser("ent_backfill_target", is_premium=True)  # simulates a row from before Task 8 shipped
check("setup: entitlement_source starts NULL, exactly like real pre-migration data",
      u_backfill.entitlement_source is None)

from app.database import _run_migrations
_run_migrations()  # safe to re-run on every boot, per its own docstring

u_backfill = refresh(u_backfill)
check("the backfill UPDATE (database.py) populates entitlement_source='complimentary' "
      "for a real pre-existing is_premium=True row — not just the resolver's in-memory "
      "fallback, the underlying data itself gets fixed",
      u_backfill.entitlement_source == "complimentary", u_backfill.entitlement_source)

# Re-running it again must be a true no-op (idempotent) — must not, for
# instance, touch a row that was legitimately later set to 'paid'.
u_paid_after = mkuser("ent_backfill_paid_after", is_premium=False, entitlement_source="paid",
                       premium_until=NOW + datetime.timedelta(days=10))
_run_migrations()
u_paid_after = refresh(u_paid_after)
check("the backfill never touches a row that already has a real entitlement_source",
      u_paid_after.entitlement_source == "paid")


# ═══════════════════════════════════════════════════════════════════════
section("J — audit fix: paid and complimentary expiries no longer share/clobber "
        "the one premium_until column")
# ═══════════════════════════════════════════════════════════════════════

# NOTE: expiry checks below compare relative order (which stored value is
# bigger/smaller/equal) rather than reconstructing an "expected" datetime via
# NOW+timedelta independently — the webhook converts via ms-epoch UTC
# (_ms_to_naive_utc), and re-deriving the same instant with naive
# datetime.timestamp() on this machine (UTC+2) double-applies a local-tz
# offset, which is a test-fixture bug, not a product one (the same class of
# bug already documented above for TODAY/YDAY-style constants elsewhere).

def days_ms(n):
    return int((NOW + datetime.timedelta(days=n)).timestamp() * 1000)

# J1 — a PAID grant must not erase an already-active COMP's expiry, and vice
# versa: each source keeps its own column, premium_until is just their max.
u_j1 = mkuser("wh_j1")
webhook({"id": "evt-j1a", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_j1",
         "store": "PROMOTIONAL", "expiration_at_ms": days_ms(5), "event_timestamp_ms": 1000})
u_j1 = refresh(u_j1)
comp_after_grant1 = u_j1.complimentary_until
webhook({"id": "evt-j1b", "type": "INITIAL_PURCHASE", "app_user_id": "wh_j1",
         "store": "APP_STORE", "expiration_at_ms": days_ms(30), "event_timestamp_ms": 2000})
u_j1 = refresh(u_j1)
check("comp expiry survives a LATER, unrelated paid grant, byte-for-byte unchanged",
      u_j1.complimentary_until == comp_after_grant1, (u_j1.complimentary_until, comp_after_grant1))
check("paid expiry is independently recorded and is later than the comp's",
      u_j1.paid_premium_until is not None and u_j1.paid_premium_until > u_j1.complimentary_until,
      (u_j1.paid_premium_until, u_j1.complimentary_until))
check("premium_until reflects the LATER of the two active expiries (the paid one)",
      u_j1.premium_until == u_j1.paid_premium_until, (u_j1.premium_until, u_j1.paid_premium_until))

# J2 — the CORE audit bug: a PAID subscription's EXPIRATION must not erase a
# still-active, unrelated COMP's expiry (previously: unconditional premium_until
# overwrite at the top of the EXPIRATION branch clobbered it regardless of source).
u_j2 = mkuser("wh_j2")
webhook({"id": "evt-j2a", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_j2",
         "store": "PROMOTIONAL", "expiration_at_ms": days_ms(60), "event_timestamp_ms": 1000})
u_j2 = refresh(u_j2)
comp_before_expiry = u_j2.complimentary_until
webhook({"id": "evt-j2b", "type": "INITIAL_PURCHASE", "app_user_id": "wh_j2",
         "store": "APP_STORE", "expiration_at_ms": days_ms(10), "event_timestamp_ms": 2000})
webhook({"id": "evt-j2c", "type": "EXPIRATION", "app_user_id": "wh_j2", "store": "APP_STORE",
         "expiration_at_ms": days_ms(10), "event_timestamp_ms": 3000})
u_j2 = refresh(u_j2)
check("the PAID subscription's own expiration is recorded on paid_premium_until "
      "(earlier than the comp's, since it was the 10-day grant)",
      u_j2.paid_premium_until is not None and u_j2.paid_premium_until < comp_before_expiry)
check("the UNRELATED comp (60 days out) is completely untouched, byte-for-byte, by the paid expiration",
      u_j2.complimentary_until == comp_before_expiry, (u_j2.complimentary_until, comp_before_expiry))
check("premium_until correctly still reflects the still-active comp, "
      "not the just-lapsed paid window — access must stay premium",
      u_j2.premium_until == u_j2.complimentary_until, (u_j2.premium_until, u_j2.complimentary_until))
check("access remains premium via the surviving comp", resolve_entitlement(u_j2)["access"] == "premium")

# J3 — mirror: a COMP's EXPIRATION must not erase a still-active, unrelated PAID window.
u_j3 = mkuser("wh_j3")
webhook({"id": "evt-j3a", "type": "INITIAL_PURCHASE", "app_user_id": "wh_j3",
         "store": "APP_STORE", "expiration_at_ms": days_ms(45), "event_timestamp_ms": 1000})
u_j3 = refresh(u_j3)
paid_before_expiry = u_j3.paid_premium_until
webhook({"id": "evt-j3b", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_j3",
         "store": "PROMOTIONAL", "expiration_at_ms": days_ms(3), "event_timestamp_ms": 2000})
webhook({"id": "evt-j3c", "type": "EXPIRATION", "app_user_id": "wh_j3", "store": "PROMOTIONAL",
         "expiration_at_ms": days_ms(3), "event_timestamp_ms": 3000})
u_j3 = refresh(u_j3)
check("the still-active PAID window (45 days out) survives the comp's own expiration, "
      "byte-for-byte unchanged",
      u_j3.paid_premium_until == paid_before_expiry, (u_j3.paid_premium_until, paid_before_expiry))
check("premium_until correctly reflects the surviving paid window",
      u_j3.premium_until == u_j3.paid_premium_until, (u_j3.premium_until, u_j3.paid_premium_until))
check("access remains premium via the surviving paid subscription",
      resolve_entitlement(u_j3)["access"] == "premium" and resolve_entitlement(u_j3)["source"] == "paid")

# J4 — sync-premium (third write path) also uses source-specific columns, not
# a direct premium_until write, and recomputes correctly alongside a comp.
u_j4 = mkuser("sync_j4")
webhook({"id": "evt-j4a", "type": "NON_RENEWING_PURCHASE", "app_user_id": "sync_j4",
         "store": "PROMOTIONAL", "expiration_at_ms": days_ms(90), "event_timestamp_ms": 1000})
u_j4 = refresh(u_j4)
comp_before_sync = u_j4.complimentary_until
with mock.patch("app.routers.auth.requests") as MockRequests4, \
     mock.patch("app.routers.auth.settings") as MockSettings4:
    MockSettings4.revenuecat_secret_api_key = "sk_test"
    MockRequests4.get.return_value.raise_for_status = lambda: None
    MockRequests4.get.return_value.json.return_value = {
        "subscriber": {"entitlements": {"Nibbler Pro": {
            "expires_date": (NOW + datetime.timedelta(days=7)).isoformat() + "Z",
            "product_identifier": "nibbler_pro_monthly",
        }}}
    }
    auth_router.sync_premium(current_user=u_j4, db=db)
u_j4 = refresh(u_j4)
check("sync-premium's paid write doesn't clobber the pre-existing comp, byte-for-byte unchanged",
      u_j4.complimentary_until == comp_before_sync, (u_j4.complimentary_until, comp_before_sync))
check("sync-premium wrote its own paid_premium_until, earlier than the 90-day comp",
      u_j4.paid_premium_until is not None and u_j4.paid_premium_until < u_j4.complimentary_until)
check("premium_until reflects the later (comp) of the two",
      u_j4.premium_until == u_j4.complimentary_until, (u_j4.premium_until, u_j4.complimentary_until))


# ═══════════════════════════════════════════════════════════════════════
section("K — Codex audit finding: reverse-order grant must not let a stale "
        "entitlement_source outrank the EXPIRATION event's own store field")
# ═══════════════════════════════════════════════════════════════════════

# K1 — the exact reported sequence: PAID granted first, COMPLIMENTARY granted
# afterward (entitlement_source is now "complimentary", stale relative to
# which grant is about to expire), then the PAID subscription's own
# EXPIRATION arrives (store=APP_STORE). A fallback that trusted
# entitlement_source over the event's own store misread this as the comp
# ending — wiping the still-valid comp and never expiring the lapsed paid
# access. The event's own store must win.
u_k1 = mkuser("wh_k1")
webhook({"id": "evt-k1a", "type": "INITIAL_PURCHASE", "app_user_id": "wh_k1",
         "store": "APP_STORE", "expiration_at_ms": days_ms(30), "event_timestamp_ms": 1000})
webhook({"id": "evt-k1b", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_k1",
         "store": "PROMOTIONAL", "expiration_at_ms": days_ms(90), "event_timestamp_ms": 2000})
u_k1 = refresh(u_k1)
check("setup: entitlement_source is 'complimentary' (the later grant) ahead of the paid expiration",
      u_k1.entitlement_source == "complimentary", u_k1.entitlement_source)
comp_before_k1 = u_k1.complimentary_until
webhook({"id": "evt-k1c", "type": "EXPIRATION", "app_user_id": "wh_k1", "store": "APP_STORE",
         "expiration_at_ms": days_ms(30), "event_timestamp_ms": 3000})
u_k1 = refresh(u_k1)
check("the 90-day comp is NOT wiped by the paid subscription's own expiration, "
      "byte-for-byte unchanged, despite entitlement_source having said 'complimentary'",
      u_k1.complimentary_until == comp_before_k1, (u_k1.complimentary_until, comp_before_k1))
check("the paid subscription's own expiration IS recorded on paid_premium_until",
      u_k1.paid_premium_until is not None and u_k1.paid_premium_until < u_k1.complimentary_until,
      (u_k1.paid_premium_until, u_k1.complimentary_until))
check("premium_until correctly reflects the surviving comp (the paid grant genuinely lapsed)",
      u_k1.premium_until == u_k1.complimentary_until, (u_k1.premium_until, u_k1.complimentary_until))
check("access remains premium via the surviving comp, source correctly re-points to complimentary",
      resolve_entitlement(u_k1)["access"] == "premium" and u_k1.entitlement_source == "complimentary",
      (resolve_entitlement(u_k1)["access"], u_k1.entitlement_source))

# K2 — mirror: COMP granted first, PAID granted afterward (entitlement_source
# now "paid"), then the COMP's own EXPIRATION arrives (store=PROMOTIONAL).
# The event's own store must still win even though it happens to already
# agree it's not the currently-recorded source's expiration.
u_k2 = mkuser("wh_k2")
webhook({"id": "evt-k2a", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_k2",
         "store": "PROMOTIONAL", "expiration_at_ms": days_ms(5), "event_timestamp_ms": 1000})
webhook({"id": "evt-k2b", "type": "INITIAL_PURCHASE", "app_user_id": "wh_k2",
         "store": "APP_STORE", "expiration_at_ms": days_ms(60), "event_timestamp_ms": 2000})
u_k2 = refresh(u_k2)
check("setup: entitlement_source is 'paid' (the later grant) ahead of the comp's expiration",
      u_k2.entitlement_source == "paid", u_k2.entitlement_source)
paid_before_k2 = u_k2.paid_premium_until
webhook({"id": "evt-k2c", "type": "EXPIRATION", "app_user_id": "wh_k2", "store": "PROMOTIONAL",
         "expiration_at_ms": days_ms(5), "event_timestamp_ms": 3000})
u_k2 = refresh(u_k2)
check("the 60-day paid window is NOT wiped by the comp's own expiration, byte-for-byte unchanged",
      u_k2.paid_premium_until == paid_before_k2, (u_k2.paid_premium_until, paid_before_k2))
check("the comp's own expiration IS recorded on complimentary_until",
      u_k2.complimentary_until is not None and u_k2.complimentary_until < u_k2.paid_premium_until)
check("premium_until correctly reflects the surviving paid window",
      u_k2.premium_until == u_k2.paid_premium_until, (u_k2.premium_until, u_k2.paid_premium_until))
check("access remains premium via the surviving paid subscription, source correctly re-points to paid",
      resolve_entitlement(u_k2)["access"] == "premium" and u_k2.entitlement_source == "paid",
      (resolve_entitlement(u_k2)["access"], u_k2.entitlement_source))

# K3 — store genuinely absent on the EXPIRATION payload falls back to the
# defensive entitlement_source check (only reachable path for that fallback
# now — proves it still exists for a malformed/incomplete RC payload).
u_k3 = mkuser("wh_k3")
webhook({"id": "evt-k3a", "type": "NON_RENEWING_PURCHASE", "app_user_id": "wh_k3",
         "store": "PROMOTIONAL", "expiration_at_ms": days_ms(20), "event_timestamp_ms": 1000})
u_k3 = refresh(u_k3)
comp_before_k3 = u_k3.complimentary_until
webhook({"id": "evt-k3b", "type": "EXPIRATION", "app_user_id": "wh_k3", "event_timestamp_ms": 2000})
u_k3 = refresh(u_k3)
check("store-absent EXPIRATION falls back to entitlement_source and still correctly "
      "expires the comp (only active source)",
      u_k3.complimentary_until != comp_before_k3, (u_k3.complimentary_until, comp_before_k3))
check("access correctly drops once the comp (only source) is expired via the fallback",
      resolve_entitlement(u_k3)["access"] != "premium")


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
if failures:
    print(f"FAILED: {len(failures)} check(s) — {failures}")
    sys.exit(1)
print("All Task 8 entitlement-unification checks passed.")
sys.exit(0)
