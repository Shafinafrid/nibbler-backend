"""
Deletion grace period (Aug 2026) — 24-hour account-deletion grace period.

  A — DELETE /auth/me first call SCHEDULES deletion (empty identity, User row
      survives, response carries scheduled_for) instead of running cleanup.
  B — a 'scheduled' erasure does NOT trip the fail-closed gate — the account
      stays fully usable for the whole grace period.
  C — GET /auth/deletion-status reports scheduled state correctly.
  D — a repeated DELETE /auth/me call while still 'scheduled' is idempotent —
      it does NOT reset the grace-period timer.
  E — POST /auth/cancel-deletion removes a 'scheduled' row with zero trace;
      cancelling with nothing scheduled is a safe no-op.
  F — promote_scheduled_erasures respects the grace window: not yet elapsed
      -> untouched; elapsed -> promoted to 'pending' with a real identity.
  G — the promoted identity is captured FRESH at promotion time, not stale
      from the moment deletion was first requested — a book added DURING
      the grace period is still included.
  H — once promoted, the account IS gated again (fail-closed, matching
      pre-grace-period behavior) and the existing cleanup machinery completes
      the erasure exactly as before.
  I — cancel_deletion is a safe no-op even if somehow called on a non-
      'scheduled' row (defense in depth beyond the gate).
  J — the scheduling email says "scheduled"/"cancel", never "permanently
      deleted" — the wrong copy here would be a real regression.
  K — config default is 24 hours.
  L — cancel_deletion and promote_scheduled_erasures both row-lock
      (.with_for_update()) against each other, matching the codebase's
      established lock-order convention.
  M — founder decision: scheduling AND cancellation are both logged to the
      Google Sheet audit trail (not just eventual completion/failure);
      cancellation's Sheet write happens strictly AFTER the Postgres row
      is gone (never while its lock is held); and the missing-commit bug
      found while building this (sync_erasure_to_sheet's row-number
      allocation silently never persisted) is fixed and proven.

Network stays hermetically blank throughout; artifact-class cleanup calls
(S3/Pinecone/Firebase/RevenueCat/Mixpanel) are mocked only in Section H,
which specifically exercises the post-promotion cleanup handoff.
"""
import os
import sys
import tempfile
import datetime
import inspect
import re
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task9.db", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports

failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def section(t):
    print(f"\n=== {t} ===")


from app.database import create_tables, SessionLocal
from app.models.user import User
from app.models.library import LibraryItem, AccountErasure
from app.middleware.auth import _erasure_gate
from app.routers import auth as auth_router
from app.services import entitlement_service as ent
from app.config import get_settings
from fastapi import HTTPException as _HTTPException

create_tables()
db = SessionLocal()
settings = get_settings()

NOW = datetime.datetime.utcnow()
GRACE = datetime.timedelta(hours=settings.account_deletion_grace_hours)


def mkuser(uid, **kw):
    kw.setdefault("email", f"{uid}@example.com")
    kw.setdefault("created_at", NOW)
    u = User(id=uid, **kw)
    db.add(u)
    db.commit()
    return u


# ═══════════════════════════════════════════════════════════════════════
section("K — config default")
# ═══════════════════════════════════════════════════════════════════════
check("account_deletion_grace_hours defaults to 24", settings.account_deletion_grace_hours == 24,
      settings.account_deletion_grace_hours)


# ═══════════════════════════════════════════════════════════════════════
section("A — DELETE /auth/me first call schedules, does not execute")
# ═══════════════════════════════════════════════════════════════════════

u_a = mkuser("sched_a")
with mock.patch("app.routers.auth._send_email_sync", return_value=True) as MockEmailA:
    res_a = auth_router.delete_account(u_a, db)

check("response reports scheduled=True", res_a.get("scheduled") is True, res_a)
check("response reports complete=False", res_a.get("complete") is False, res_a)
check("response carries a scheduled_for timestamp", bool(res_a.get("scheduled_for")), res_a)

erasure_a = db.query(AccountErasure).filter(AccountErasure.user_id == "sched_a").first()
check("erasure row created in 'scheduled' state", erasure_a is not None and erasure_a.state == "scheduled",
      erasure_a.state if erasure_a else None)
check("identity is NOT captured yet (empty) — avoids going stale during the grace period",
      erasure_a.identity == {}, erasure_a.identity)

db.expire_all()
still_there = db.query(User).filter(User.id == "sched_a").first()
check("the User row SURVIVES scheduling — nothing external or internal was actually touched",
      still_there is not None)

check("the confirmation email was sent synchronously (not deferred)", MockEmailA.called)


# ═══════════════════════════════════════════════════════════════════════
section("B — a 'scheduled' erasure does not trip the fail-closed gate")
# ═══════════════════════════════════════════════════════════════════════

gate_raised_b = False
try:
    _erasure_gate(db, "sched_a")
except _HTTPException:
    gate_raised_b = True
check("the account stays fully usable during the grace period — the gate does NOT block it",
      gate_raised_b is False)


# ═══════════════════════════════════════════════════════════════════════
section("C — GET /auth/deletion-status")
# ═══════════════════════════════════════════════════════════════════════

status_a = auth_router.deletion_status(u_a, db)
check("reports scheduled=True for the account that just scheduled deletion",
      status_a.get("scheduled") is True, status_a)
check("scheduled_for matches the erasure row's requested_at + grace period",
      status_a.get("scheduled_for") == (erasure_a.requested_at + GRACE).isoformat(),
      (status_a.get("scheduled_for"), (erasure_a.requested_at + GRACE).isoformat()))
check("audit fix: grace_period_hours is included alongside a real scheduled deletion "
      "(the mobile pre-confirmation dialog reads this instead of a hardcoded number)",
      status_a.get("grace_period_hours") == settings.account_deletion_grace_hours, status_a)

u_c_fresh = mkuser("no_erasure_c")
status_c_fresh = auth_router.deletion_status(u_c_fresh, db)
check("an account with nothing scheduled reports scheduled=False",
      status_c_fresh.get("scheduled") is False, status_c_fresh)
check("audit fix: grace_period_hours is included EVEN when nothing is scheduled — the "
      "pre-confirmation dialog fires before any deletion exists to report on",
      status_c_fresh.get("grace_period_hours") == settings.account_deletion_grace_hours, status_c_fresh)


# ═══════════════════════════════════════════════════════════════════════
section("D — a repeated DELETE /auth/me call does not reset the timer")
# ═══════════════════════════════════════════════════════════════════════

requested_at_before = erasure_a.requested_at
with mock.patch("app.routers.auth._send_email_sync", return_value=True) as MockEmailD:
    res_d = auth_router.delete_account(u_a, db)
db.expire_all()
erasure_a_after = db.query(AccountErasure).filter(AccountErasure.user_id == "sched_a").first()

check("the idempotent repeat still reports scheduled=True", res_d.get("scheduled") is True, res_d)
check("requested_at is UNCHANGED — tapping delete twice does not push the deletion date out",
      erasure_a_after.requested_at == requested_at_before,
      (erasure_a_after.requested_at, requested_at_before))
check("no second confirmation email is sent on the idempotent repeat", not MockEmailD.called)


# ═══════════════════════════════════════════════════════════════════════
section("E — POST /auth/cancel-deletion")
# ═══════════════════════════════════════════════════════════════════════

cancel_res = auth_router.cancel_deletion(u_a, db)
check("cancelling a genuinely-scheduled deletion succeeds", cancel_res.get("cancelled") is True, cancel_res)

db.expire_all()
check("the erasure row is genuinely gone — zero trace left",
      db.query(AccountErasure).filter(AccountErasure.user_id == "sched_a").first() is None)
check("deletion-status now reports scheduled=False",
      auth_router.deletion_status(u_a, db).get("scheduled") is False)

cancel_again = auth_router.cancel_deletion(u_a, db)
check("cancelling again with nothing scheduled is a safe no-op, not an error",
      cancel_again == {"cancelled": False, "message": "No scheduled deletion to cancel."}, cancel_again)


# ═══════════════════════════════════════════════════════════════════════
section("F — promote_scheduled_erasures respects the grace window")
# ═══════════════════════════════════════════════════════════════════════

u_f = mkuser("promote_f")
with mock.patch("app.routers.auth._send_email_sync", return_value=True):
    auth_router.delete_account(u_f, db)

promoted_early = ent.promote_scheduled_erasures(db)
db.expire_all()
erasure_f = db.query(AccountErasure).filter(AccountErasure.user_id == "promote_f").first()
check("nothing is promoted while the grace period has not elapsed",
      promoted_early == 0 and erasure_f.state == "scheduled", (promoted_early, erasure_f.state))
check("identity remains empty while still within the grace window",
      erasure_f.identity == {}, erasure_f.identity)

# Backdate past the grace period, matching how the real scheduler discovers it.
erasure_f.requested_at = NOW - GRACE - datetime.timedelta(minutes=1)
db.commit()

promoted_late = ent.promote_scheduled_erasures(db)
db.expire_all()
erasure_f_after = db.query(AccountErasure).filter(AccountErasure.user_id == "promote_f").first()
check("exactly one row is promoted once its grace period has elapsed", promoted_late == 1, promoted_late)
check("the row is now 'pending', handing off to the existing retry_account_erasures machinery",
      erasure_f_after.state == "pending", erasure_f_after.state)
check("a real identity was captured — every expected key present",
      set(erasure_f_after.identity.keys()) == {
          "source_keys", "image_keys", "avatar_key", "pinecone_namespace",
          "cleanup_ledger_ids", "firebase_uid", "snapshot",
      }, sorted(erasure_f_after.identity.keys()))


# ═══════════════════════════════════════════════════════════════════════
section("G — promoted identity is captured FRESH, not stale from request time")
# ═══════════════════════════════════════════════════════════════════════

u_g = mkuser("fresh_identity_g")
with mock.patch("app.routers.auth._send_email_sync", return_value=True):
    auth_router.delete_account(u_g, db)

# The account is fully usable during the grace period — simulate the user
# uploading a book AFTER scheduling deletion but BEFORE the window elapses.
db.add(LibraryItem(
    id="book_added_during_grace", user_id="fresh_identity_g", title="Added mid-grace",
    type="pdf", processed=True, file_url="fresh_identity_g/book_added_during_grace/file.pdf",
))
db.commit()

erasure_g = db.query(AccountErasure).filter(AccountErasure.user_id == "fresh_identity_g").first()
erasure_g.requested_at = NOW - GRACE - datetime.timedelta(minutes=1)
db.commit()

ent.promote_scheduled_erasures(db)
db.expire_all()
erasure_g_after = db.query(AccountErasure).filter(AccountErasure.user_id == "fresh_identity_g").first()
check("a book added DURING the grace period IS included in the promoted identity — "
      "proves identity is captured fresh at promotion time, not frozen at request time",
      "fresh_identity_g/book_added_during_grace/file.pdf" in erasure_g_after.identity["source_keys"],
      erasure_g_after.identity["source_keys"])


# ═══════════════════════════════════════════════════════════════════════
section("H — once promoted, the account is gated again and cleanup completes normally")
# ═══════════════════════════════════════════════════════════════════════

gate_raised_h = False
try:
    _erasure_gate(db, "fresh_identity_g")
except _HTTPException as e:
    gate_raised_h = True
    gate_code_h = e.detail.get("code") if isinstance(e.detail, dict) else None
check("a 'pending' post-promotion erasure DOES trip the fail-closed gate — matches pre-grace-period behavior",
      gate_raised_h and gate_code_h == "account_erasure_pending")

with mock.patch("app.routers.auth.EmbeddingService") as MockEmbedH, \
     mock.patch("app.routers.auth.S3Service") as MockS3H, \
     mock.patch("firebase_admin.auth.delete_user") as MockFirebaseH, \
     mock.patch("app.routers.auth._delete_revenuecat_subscriber", return_value=True), \
     mock.patch("app.routers.auth._delete_mixpanel_profile_sync", return_value=True), \
     mock.patch("app.routers.auth._send_email_sync", return_value=True):
    MockEmbedH.return_value.delete_user_namespace.return_value = True
    MockS3H.return_value.delete_file.return_value = True
    MockFirebaseH.return_value = None
    res_h = auth_router.delete_account(u_g, db)

check("the existing cleanup machinery completes the erasure exactly as before this grace period was added",
      res_h.get("complete") is True, res_h)
db.expire_all()
check("the User row is genuinely gone once the whole flow (schedule -> promote -> cleanup) finishes",
      db.query(User).filter(User.id == "fresh_identity_g").first() is None)


# ═══════════════════════════════════════════════════════════════════════
section("I — cancel_deletion is a safe no-op on a non-'scheduled' row (defense in depth)")
# ═══════════════════════════════════════════════════════════════════════

u_i = mkuser("cancel_pending_i")
db.add(AccountErasure(id="erasure-i1", user_id="cancel_pending_i", state="pending", identity={
    "source_keys": [], "image_keys": [], "avatar_key": None,
    "pinecone_namespace": "cancel_pending_i", "cleanup_ledger_ids": [],
    "firebase_uid": "cancel_pending_i",
}))
db.commit()

cancel_i = auth_router.cancel_deletion(u_i, db)
check("cancel_deletion refuses to touch a 'pending' row (cleanup already underway), "
      "even if somehow called directly, bypassing the gate that would normally block it first",
      cancel_i == {"cancelled": False, "message": "No scheduled deletion to cancel."}, cancel_i)
db.expire_all()
check("the 'pending' erasure row is untouched",
      db.query(AccountErasure).filter(AccountErasure.id == "erasure-i1").first() is not None)


# ═══════════════════════════════════════════════════════════════════════
section("J — the scheduling email's copy is correct, not the old immediate-deletion wording")
# ═══════════════════════════════════════════════════════════════════════

u_j = mkuser("email_copy_j")
with mock.patch("app.routers.auth._send_email_sync", return_value=True) as MockEmailJ:
    auth_router.delete_account(u_j, db)

email_kwargs = MockEmailJ.call_args.kwargs
check("subject mentions 'scheduled', not an already-completed deletion",
      "scheduled" in email_kwargs.get("subject", "").lower(), email_kwargs.get("subject"))
check("body mentions cancelling", "cancel" in email_kwargs.get("html", "").lower())
check("body does NOT claim the account has ALREADY been permanently deleted "
      "(the old, wrong, immediate-deletion wording) — 'scheduled to be permanently "
      "deleted on <date>' is correct future-tense wording and is fine",
      "has been permanently deleted" not in email_kwargs.get("html", "").lower(), email_kwargs.get("html"))


# ═══════════════════════════════════════════════════════════════════════
section("L — row-locking source guardrail")
# ═══════════════════════════════════════════════════════════════════════
# Audit finding (Aug 2026): this whole file runs on SQLite, which SILENTLY
# DROPS `.with_for_update()` — no error, no warning, the emitted SQL is a
# plain unlocked SELECT. So these checks prove the locking call and the
# post-lock state re-check are PRESENT IN SOURCE, and that
# promote_scheduled_erasures releases the lock (commits its claim) BEFORE
# the slow RevenueCat network call rather than holding it across I/O — they
# do NOT prove real two-thread concurrent safety, which this harness cannot
# demonstrate at all for a locking clause it never actually sends. That
# matches this codebase's own established precedent for the identical
# limitation (see test_task8_entitlement_unification.py's F3 section) —
# real-Postgres proof of this general claim-lease pattern exists for
# OTHER queues (test_task2_pg_harness.py), not yet for this specific
# cancel-vs-promote interleaving.

cancel_src = inspect.getsource(auth_router.cancel_deletion)
promote_src = inspect.getsource(ent.promote_scheduled_erasures)
check("cancel_deletion row-locks its query (source-presence only — see note above)",
      ".with_for_update()" in cancel_src)
check("cancel_deletion re-checks state AFTER acquiring the lock (not before)",
      "erasure.state != \"scheduled\"" in cancel_src)
check("promote_scheduled_erasures row-locks its query (source-presence only — see note above)",
      ".with_for_update()" in promote_src)
check("promote_scheduled_erasures re-checks state AFTER acquiring the lock (not before)",
      'erasure.state != "scheduled"' in promote_src)
check("audit fix: promote_scheduled_erasures commits its claim BEFORE calling "
      "_capture_erasure_identity (a real requests.get(...) to RevenueCat) — the row lock "
      "must never be held across live network I/O, matching retry_account_erasures' own "
      "established discipline just above it in this same file",
      re.search(r"erasure\.claimed_until = datetime\.utcnow\(\) \+ claim_ttl\s*\n\s*db\.commit\(\)"
                r"\s*\n\s*\n\s*identity = _capture_erasure_identity\(db, user\)", promote_src) is not None,
      promote_src)


# ═══════════════════════════════════════════════════════════════════════
section("M — Sheet audit trail: scheduling, cancellation, and the missing-commit fix")
# ═══════════════════════════════════════════════════════════════════════

# M1 — scheduling itself is logged, not just the eventual outcome.
u_m1 = mkuser("sheet_audit_m1")
with mock.patch("app.routers.auth.deletion_sheets_service.sync_erasure_to_sheet") as MockSyncM1:
    MockSyncM1.return_value = True
    auth_router.delete_account(u_m1, db)

check("scheduling calls sync_erasure_to_sheet with a 'scheduled' erasure — a cancelled "
      "deletion is no longer invisible to the audit trail",
      MockSyncM1.called and MockSyncM1.call_args.args[0].state == "scheduled",
      MockSyncM1.call_args.args[0].state if MockSyncM1.called else None)

# M2 — cancellation is ALSO logged, and its Sheet write happens strictly
# AFTER the Postgres row is gone (never while cancel_deletion's row lock
# is held — the exact discipline the promotion fix above established).
row_was_gone_when_sync_ran = {"value": None}


def _check_row_gone_side_effect(erasure_arg, snapshot_arg):
    db.expire_all()
    still_there = db.query(AccountErasure).filter(AccountErasure.user_id == "sheet_audit_m1").first()
    row_was_gone_when_sync_ran["value"] = still_there is None
    return True


with mock.patch("app.routers.auth.deletion_sheets_service.sync_erasure_to_sheet",
                 side_effect=_check_row_gone_side_effect) as MockSyncM2:
    cancel_res_m2 = auth_router.cancel_deletion(u_m1, db)

check("cancellation succeeds", cancel_res_m2.get("cancelled") is True, cancel_res_m2)
check("cancellation calls sync_erasure_to_sheet with a DETACHED 'cancelled' copy",
      MockSyncM2.called and MockSyncM2.call_args.args[0].state == "cancelled",
      MockSyncM2.call_args.args[0].state if MockSyncM2.called else None)
check("audit discipline: the Postgres row was ALREADY deleted+committed by the time the "
      "Sheet network call ran — the row lock was never held across it",
      row_was_gone_when_sync_ran["value"] is True, row_was_gone_when_sync_ran)

# M3 — the missing-commit bug: sync_erasure_to_sheet's row-number mutation
# must actually persist, not just live in memory for the rest of this
# request. Verified via db.expire_all() — the only way to force a REAL
# re-read from the database (not just Python's cached attribute value)
# within a single shared test session.
u_m3 = mkuser("sheet_audit_m3", successful_sources_total=0)
db.add(AccountErasure(
    id="erasure-m3", user_id="sheet_audit_m3", state="failed",
    identity={"source_keys": [], "image_keys": [], "avatar_key": None,
              "pinecone_namespace": "sheet_audit_m3", "cleanup_ledger_ids": [],
              "firebase_uid": "sheet_audit_m3", "snapshot": auth_router._capture_erasure_snapshot(db, u_m3)},
    retry_count=1,
))
db.commit()
erasure_m3 = db.query(AccountErasure).filter(AccountErasure.id == "erasure-m3").first()


def _simulate_allocate_side_effect(erasure_arg, snapshot_arg):
    # Mirrors what the REAL _allocate_sheet_rows does: assign a row number
    # only if not already set.
    if erasure_arg.sheet_row is None:
        erasure_arg.sheet_row = 42
    return True


with mock.patch("app.routers.auth.EmbeddingService") as MockEmbedM3, \
     mock.patch("app.routers.auth.S3Service") as MockS3M3, \
     mock.patch("firebase_admin.auth.delete_user", side_effect=RuntimeError("still down")), \
     mock.patch("app.routers.auth._delete_revenuecat_subscriber", return_value=True), \
     mock.patch("app.routers.auth._delete_mixpanel_profile_sync", return_value=True), \
     mock.patch("app.routers.auth._send_email_sync", return_value=True), \
     mock.patch("app.routers.auth.deletion_sheets_service.sync_erasure_to_sheet",
                 side_effect=_simulate_allocate_side_effect):
    MockEmbedM3.return_value.delete_user_namespace.return_value = True
    MockS3M3.return_value.delete_file.return_value = True
    auth_router._attempt_account_erasure_cleanup(db, erasure_m3)

db.expire_all()  # force a REAL re-read from the database, not a cached Python attribute
erasure_m3_reloaded = db.query(AccountErasure).filter(AccountErasure.id == "erasure-m3").first()
check("audit fix: sheet_row assigned during sync_erasure_to_sheet's call GENUINELY "
      "persists (proven via expire_all() forcing a real DB re-read) — before the fix, "
      "this would come back None, silently causing every retry to allocate a brand-new "
      "row instead of updating the one already assigned",
      erasure_m3_reloaded.sheet_row == 42, erasure_m3_reloaded.sheet_row)


# ═══════════════════════════════════════════════════════════════════════
print(f"\n{'='*60}")
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All deletion-grace-period checks passed.")
sys.exit(0)
