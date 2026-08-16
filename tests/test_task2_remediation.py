"""
Task 2 remediation (Aug 2026) — behavioral tests for every SQLite-testable
finding from Hermes's audit that test_task2_entitlement.py doesn't already
cover: the reservation primitive (reserve/convert/release/expire), Premium-
only uploads never consuming a Free slot, wall-clock expiry reconciliation
(injected clock — timestamps are backdated, never slept on), the OCR lock
guard + OCR reservation, GET /bites/daily lock filtering, the scheduler's
locked-source unread-hold exclusion, and fail-closed migration/backfill
accounting.

Real-PostgreSQL-only concerns (the same-item/different-item completion
races, and the actual ALTER TABLE / advisory-lock / NOT NULL behavior) are
NOT here — SQLite cannot exercise real row-level locking or that DDL
meaningfully (see this repo's existing convention). They were validated
separately against a disposable local Postgres 16 instance; raw output is
in the Task 2 remediation completion report. This file stays credential-free
and makes no paid-provider call — EmbeddingService/ocr_service are exercised
only through their existing keyless/mock-mode behavior in this environment.
"""
import os, sys, tempfile, datetime, unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/r.db", CLAUDE_API_KEY="t", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

from app.database import create_tables, SessionLocal
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite
from app.services import entitlement_service as ent

create_tables()
db = SessionLocal()

NOW = datetime.datetime.utcnow()
OLD = NOW - datetime.timedelta(days=400)  # well outside the 7-day signup trial


def mkuser(uid, **kw):
    kw.setdefault("created_at", OLD)
    kw.setdefault("email", f"{uid}@example.com")
    u = User(id=uid, **kw)
    db.add(u); db.commit()
    return u


def mkitem(iid, uid, **kw):
    kw.setdefault("type", "pdf")
    kw.setdefault("title", iid)
    kw.setdefault("processed", True)  # matches test_task2_entitlement.py's mkitem convention
    it = LibraryItem(id=iid, user_id=uid, **kw)
    db.add(it); db.commit()
    return it


def refresh_user(uid):
    db.expire_all()
    return db.query(User).filter(User.id == uid).first()


def refresh_item(iid):
    db.expire_all()
    return db.query(LibraryItem).filter(LibraryItem.id == iid).first()


# ─────────────────────────────────────────────────────────────────────────
section("Reservation primitive — acquire, idempotent retry, capacity denial")
# ─────────────────────────────────────────────────────────────────────────
u = mkuser("res_free", successful_sources_total=0)
i1 = mkitem("res1", "res_free", processed=False)
ok = ent.reserve_free_capacity(db, i1, "res_free")
u = refresh_user("res_free")
check("first reservation for a Free user succeeds and marks 'pending'",
      ok and refresh_item("res1").entitlement_status == "pending")
check("reserving increments reserved_sources_count by exactly 1", u.reserved_sources_count == 1)

ok_retry = ent.reserve_free_capacity(db, refresh_item("res1"), "res_free")
u = refresh_user("res_free")
check("retrying the SAME reservation is idempotent — no double increment",
      ok_retry and u.reserved_sources_count == 1)

i2 = mkitem("res2", "res_free", processed=False)
i3 = mkitem("res3", "res_free", processed=False)
ent.reserve_free_capacity(db, i2, "res_free")
ent.reserve_free_capacity(db, i3, "res_free")
u = refresh_user("res_free")
check("three reservations for a Free user at 0/3 all succeed", u.reserved_sources_count == 3)

i4 = mkitem("res4", "res_free", processed=False)
ok4 = ent.reserve_free_capacity(db, i4, "res_free")
check("a 4th reservation at 3/3 (all pending, none consumed yet) is DENIED before any paid call",
      ok4 is False and refresh_item("res4").entitlement_status == "rejected")
check("the denied item carries a readable processing_error", bool(refresh_item("res4").processing_error))

section("Reservation → conversion (finalize) and → release (failure)")
ok_fin = ent.finalize_successful_processing(db, refresh_item("res1"), "res_free", chunk_count=5)
u = refresh_user("res_free")
check("finalize converts 'pending' → 'consumed', decrements reserved, increments the permanent counter",
      ok_fin and refresh_item("res1").entitlement_status == "consumed"
      and u.reserved_sources_count == 2 and u.successful_sources_total == 1)

ent.release_reservation(db, refresh_item("res2"), "res_free", reason="embedding failed")
u = refresh_user("res_free")
check("a failed item's reservation is released — status 'released', reserved count drops, nothing consumed",
      refresh_item("res2").entitlement_status == "released"
      and u.reserved_sources_count == 1 and u.successful_sources_total == 1)

ok_after_release = ent.reserve_free_capacity(db, refresh_item("res2"), "res_free")
u = refresh_user("res_free")
check("the released slot can be re-reserved (a retry after a real failure is not permanently stuck)",
      ok_after_release and refresh_item("res2").entitlement_status == "pending"
      and u.reserved_sources_count == 2)

section("Premium-exempt reservation — no cap, never touches the Free counter")
u_pro = mkuser("res_pro", premium_until=NOW + datetime.timedelta(days=10), successful_sources_total=0)
for n in range(6):
    it = mkitem(f"pro_item_{n}", "res_pro", processed=False)
    ok_pro = ent.reserve_free_capacity(db, it, "res_pro")
    check(f"Premium reservation #{n+1} is unconditionally accepted", ok_pro)
u_pro = refresh_user("res_pro")
check("Premium reservations never touch reserved_sources_count or successful_sources_total",
      u_pro.reserved_sources_count == 0 and u_pro.successful_sources_total == 0)
for n in range(6):
    ent.finalize_successful_processing(db, refresh_item(f"pro_item_{n}"), "res_pro", chunk_count=1)
u_pro = refresh_user("res_pro")
check("finalizing 6 Premium-created items still leaves successful_sources_total at 0 — "
      "created-while-Premium sources are 'temporary Premium-only', never permanently consumed",
      u_pro.successful_sources_total == 0)
statuses = {n: refresh_item(f"pro_item_{n}").entitlement_status for n in range(6)}
check("every Premium-created item's entitlement_status is 'premium'",
      all(s == "premium" for s in statuses.values()), statuses)

section("Downgrade after Premium-only uploads: the retained ≤3 selection OCCUPIES permanent capacity")
# 2nd-audit remediation #1 (PG_DOWNGRADE): retaining a Premium-created source
# at downgrade IS what "occupies one of the account's three permanent Free
# entitlements" means — the FIRST version of this test asserted the
# opposite (successful_sources_total stays 0 after retaining 3 sources),
# which is EXACTLY the bug Hermes's second audit reproduced as
# `PG_DOWNGRADE 0 ['p0','p1','p2'] True`: an account could retain 3
# Premium-era sources AND still upload 3 more "free" ones, six lifetime
# slots instead of three. The fix promotes each retained not-yet-'consumed'
# item to 'consumed' and increments the counter, capped at the limit.
db.query(User).filter(User.id == "res_pro").update({"premium_until": OLD})
db.commit()
ent.reconcile_free_lock_state(db, refresh_user("res_pro"))
u_pro = refresh_user("res_pro")
unlocked = {i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == "res_pro", LibraryItem.is_unlocked_selection.is_(True))}
check("exactly 3 of the 6 Premium-only sources stay unlocked after downgrade", len(unlocked) == 3, unlocked)
check("successful_sources_total becomes 3 — the retained set now occupies all 3 permanent slots "
      "(the fix for PG_DOWNGRADE 0 [...] True)",
      u_pro.successful_sources_total == 3, u_pro.successful_sources_total)
check("can_accept_new_upload is now False — no 3 additional 'free' uploads on top of the 3 retained",
      ent.can_accept_new_upload(u_pro) is False)
check("the 3 RETAINED sources are promoted to 'consumed'; the 3 still-locked ones remain 'premium' "
      "(never independently consuming a slot they were never selected into)",
      {refresh_item(f"pro_item_{n}").entitlement_status for n in range(6) if f"pro_item_{n}" in unlocked} == {"consumed"}
      and {refresh_item(f"pro_item_{n}").entitlement_status for n in range(6) if f"pro_item_{n}" not in unlocked} == {"premium"})

section("A deleted, never-retained Premium-only source never consumed a slot")
locked_pro_ids = {f"pro_item_{n}" for n in range(6)} - unlocked
victim = next(iter(locked_pro_ids))
db.query(LibraryItem).filter(LibraryItem.id == victim).delete()
db.commit()
u_pro = refresh_user("res_pro")
check("deleting a locked, never-selected Premium-only source leaves successful_sources_total at 3 "
      "(unchanged by the deletion — it was never counted, and deleting a RETAINED source separately "
      "never restores capacity either, per remediation #1's 'delete-after-downgrade' requirement)",
      u_pro.successful_sources_total == 3, u_pro.successful_sources_total)

section("Delete-after-downgrade: deleting one of the RETAINED 3 leaves a permanent gap, replacement rejected")
retained_victim = next(iter(unlocked))
db.query(LibraryItem).filter(LibraryItem.id == retained_victim).delete()
db.commit()
u_pro = refresh_user("res_pro")
check("deleting one of the 3 RETAINED sources does not reduce successful_sources_total (permanent gap)",
      u_pro.successful_sources_total == 3, u_pro.successful_sources_total)
still_unlocked = {i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == "res_pro", LibraryItem.is_unlocked_selection.is_(True))}
check("exactly 2 sources remain unlocked — no rotation filled the gap", len(still_unlocked) == 2, still_unlocked)
new_item = mkitem("pro_replacement", "res_pro", processed=False)
ok_replacement = ent.reserve_free_capacity(db, new_item, "res_pro")
check("a replacement upload attempt is REJECTED — the account is at its permanent 3/3 limit",
      ok_replacement is False and refresh_item("pro_replacement").entitlement_status == "rejected")

section("Resubscribe then a SECOND downgrade: accounting stays monotonic, never exceeds the limit")
db.query(User).filter(User.id == "res_pro").update({"premium_until": NOW + datetime.timedelta(days=5)})
db.commit()
ent.reconcile_free_lock_state(db, refresh_user("res_pro"))
check("resubscribing snapshots entitled again (dormant — no accounting change)",
      refresh_user("res_pro").free_lock_last_effective_premium is True
      and refresh_user("res_pro").successful_sources_total == 3)
db.query(User).filter(User.id == "res_pro").update({"premium_until": OLD})
db.commit()
ent.reconcile_free_lock_state(db, refresh_user("res_pro"))
u_pro = refresh_user("res_pro")
check("the SECOND downgrade re-finalizes without pushing successful_sources_total past the limit",
      u_pro.successful_sources_total == 3, u_pro.successful_sources_total)
ent.reconcile_free_lock_state(db, refresh_user("res_pro"))  # same episode — must be a true no-op
check("duplicate reconciliation in the same episode is idempotent (no further increment)",
      refresh_user("res_pro").successful_sources_total == 3)

section("Downgrade accounting — prior Free consumption, THEN Premium, THEN a later downgrade")
# The account already permanently consumed 2 Free slots (real items, still
# owned) BEFORE ever subscribing — capacity_available at the next downgrade
# must be exactly 1 (3 - 2), not 3, even though 3+ eligible sources exist.
u_mix = mkuser("res_mix", successful_sources_total=0)
mix_f1 = mkitem("mix_f1", "res_mix", processed=False)
mix_f2 = mkitem("mix_f2", "res_mix", processed=False)
ent.reserve_free_capacity(db, mix_f1, "res_mix")
ent.finalize_successful_processing(db, refresh_item("mix_f1"), "res_mix", chunk_count=1)
ent.reserve_free_capacity(db, refresh_item("mix_f2"), "res_mix")
ent.finalize_successful_processing(db, refresh_item("mix_f2"), "res_mix", chunk_count=1)
check("res_mix has genuinely consumed 2 Free slots via the real reserve→finalize flow",
      refresh_user("res_mix").successful_sources_total == 2)
db.query(User).filter(User.id == "res_mix").update(
    {"premium_until": NOW + datetime.timedelta(days=10)})
db.commit()
# Subscribes to Premium, uploads 3 MORE sources (all 'premium'-status,
# deliberately made the most recently active so the fallback prefers them
# over mix_f1/mix_f2 — the harder, capacity-CONSTRAINED case).
for n, days_ago in [(0, 0), (1, 1), (2, 2)]:
    it = mkitem(f"mix_p{n}", "res_mix", processed=False,
                last_active_at=NOW - datetime.timedelta(hours=days_ago))
    ent.reserve_free_capacity(db, it, "res_mix")
    ent.finalize_successful_processing(db, refresh_item(f"mix_p{n}"), "res_mix", chunk_count=1)
check("all 3 new uploads are 'premium'-status (uncapped) and did not touch the counter",
      refresh_user("res_mix").successful_sources_total == 2
      and all(refresh_item(f"mix_p{n}").entitlement_status == "premium" for n in range(3)))
db.query(User).filter(User.id == "res_mix").update({"premium_until": OLD})
db.commit()
ent.reconcile_free_lock_state(db, refresh_user("res_mix"))
u_mix = refresh_user("res_mix")
check("capacity-constrained downgrade: successful_sources_total becomes exactly 3 (2 already + "
      "only 1 more slot available), never 5 — permanent consumption never exceeds the limit",
      u_mix.successful_sources_total == 3, u_mix.successful_sources_total)
mix_unlocked = {i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == "res_mix", LibraryItem.is_unlocked_selection.is_(True))}
check("exactly 3 sources are retained unlocked despite 5 total eligible sources existing",
      len(mix_unlocked) == 3, mix_unlocked)
check("the 2 already-consumed sources (mix_f1, mix_f2) are preferred for retention — they cost no "
      "new capacity — over the capacity-constrained premium candidates",
      {"mix_f1", "mix_f2"}.issubset(mix_unlocked), mix_unlocked)

# ─────────────────────────────────────────────────────────────────────────
section("Stale/crashed reservation recovery")
# ─────────────────────────────────────────────────────────────────────────
u_stale = mkuser("res_stale", successful_sources_total=1)
i_stale = mkitem("stale1", "res_stale", processed=False)
ent.reserve_free_capacity(db, i_stale, "res_stale")
# Simulate a crashed worker that never renewed its lease: back-date
# reservation_lease_expires_at (the renewable-lease remediation's staleness
# signal — NOT reserved_at, which is now audit metadata only) past the TTL
# rather than sleeping — an injected clock, not a real wait.
old_ts = NOW - datetime.timedelta(minutes=ent.RESERVATION_TTL_MINUTES + 5)
db.query(LibraryItem).filter(LibraryItem.id == "stale1").update({"reservation_lease_expires_at": old_ts})
db.commit()
i_new = mkitem("stale2", "res_stale", processed=False)
ok_new = ent.reserve_free_capacity(db, i_new, "res_stale")
check("a new reservation attempt reaps the stale 'pending' one first, freeing its slot",
      refresh_item("stale1").entitlement_status == "released")
u_stale = refresh_user("res_stale")
check("the reaped reservation no longer counts toward reserved_sources_count "
      "(1 consumed + 1 fresh pending = capacity math stays correct)",
      ok_new and u_stale.reserved_sources_count == 1)

fresh_pending = mkitem("stale3", "res_stale", processed=False)
ent.reserve_free_capacity(db, fresh_pending, "res_stale")
db.query(LibraryItem).filter(LibraryItem.id == "stale3").update(
    {"reservation_lease_expires_at": NOW + datetime.timedelta(minutes=25)})
db.commit()
i_probe = mkitem("stale4", "res_stale", processed=False)
ent.reserve_free_capacity(db, i_probe, "res_stale")
check("a reservation still well within its (renewed) lease window is NOT reaped",
      refresh_item("stale3").entitlement_status == "pending")

section("A legacy 'pending' row with NO lease expiry (pre-remediation data) still reaps via the "
        "reserved_at fallback, rather than becoming permanently un-reapable")
legacy_pending = mkitem("stale_legacy", "res_stale", processed=False,
                         entitlement_status="pending",
                         reserved_at=NOW - datetime.timedelta(minutes=ent.RESERVATION_TTL_MINUTES + 5),
                         reservation_lease_expires_at=None)
db.query(User).filter(User.id == "res_stale").update(
    {"reserved_sources_count": User.reserved_sources_count + 1})
db.commit()
i_probe2 = mkitem("stale5", "res_stale", processed=False)
ent.reserve_free_capacity(db, i_probe2, "res_stale")
check("a NULL-lease-expiry legacy 'pending' row is reaped via the reserved_at fallback",
      refresh_item("stale_legacy").entitlement_status == "released")

# ─────────────────────────────────────────────────────────────────────────
section("No paid work runs before capacity is secured — proof via a call spy")
# ─────────────────────────────────────────────────────────────────────────
from app.routers import library as library_router

u_atcap = mkuser("res_atcap", successful_sources_total=3)
item_atcap = mkitem("atcap1", "res_atcap", processed=False, content="some text")
with mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
    library_router.process_item_embeddings("atcap1", "res_atcap")
    check("EmbeddingService is NEVER constructed for an item rejected at reservation time",
          MockEmbed.call_count == 0, f"called {MockEmbed.call_count} time(s)")
check("the rejected item is never marked processed", refresh_item("atcap1").processed is False)
check("the rejected item carries the free_limit message, not silence",
      "Free includes" in (refresh_item("atcap1").processing_error or ""))

u_ok = mkuser("res_ok", successful_sources_total=0)
item_ok = mkitem("ok1", "res_ok", processed=False, content="some real text")
with mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
    MockEmbed.return_value.index_text.return_value = 4
    library_router.process_item_embeddings("ok1", "res_ok")
    check("EmbeddingService IS constructed exactly once for an item with capacity",
          MockEmbed.call_count == 1)
check("the accepted item is marked processed with the mocked chunk count",
      refresh_item("ok1").processed is True and refresh_item("ok1").chunk_count == 4)

# ─────────────────────────────────────────────────────────────────────────
section("OCR route — locked source rejected, reservation before the paid OCR call")
# ─────────────────────────────────────────────────────────────────────────
from fastapi.testclient import TestClient
from app.database import get_db
from app.middleware.auth import get_current_user
import main

CURRENT_UID = {"v": None}
def _db(): yield db
def _current_user(): return db.query(User).filter(User.id == CURRENT_UID["v"]).first()
main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = _current_user
c = TestClient(main.app)
def as_user(uid): CURRENT_UID["v"] = uid

u_ocr_locked = mkuser("ocr_locked", premium_until=OLD, successful_sources_total=0)
mkitem("ocr_lk1", "ocr_locked", is_active=False)
mkitem("ocr_lk2", "ocr_locked", is_active=False)
mkitem("ocr_lk3", "ocr_locked", is_active=False)
mkitem("ocr_lk4", "ocr_locked", is_active=False, file_url="s3://x")
ent.reconcile_free_lock_state(db, refresh_user("ocr_locked"))
locked_row = next(i for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == "ocr_locked", LibraryItem.is_unlocked_selection.is_(False)))
as_user("ocr_locked")
r = c.post(f"/library/{locked_row.id}/ocr")
check("POST /library/{id}/ocr on a locked source is refused BEFORE any OCR work (403 source_locked)",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked",
      f"{r.status_code} {r.text[:150]}")

u_ocr_cap = mkuser("ocr_atcap", successful_sources_total=3)
mkitem("ocr_cap1", "ocr_atcap", processed=False, file_url="s3://y")
# `_run_ocr` imports `ocr_service` and S3Service LOCALLY (inside the
# function), so the patch target is the service module itself, not the
# router module's namespace.
with mock.patch("app.services.ocr_service.ocr_pdf") as mock_ocr_pdf, \
     mock.patch("app.routers.library.EmbeddingService") as MockEmbed, \
     mock.patch("app.routers.library.S3Service") as MockS3:
    MockS3.return_value.download_file.return_value = b"fake-pdf-bytes"
    library_router._run_ocr("ocr_cap1", "ocr_atcap")
    check("OCR is never invoked when the Free lifetime cap is already reached",
          mock_ocr_pdf.call_count == 0)
    check("no embedding call either — capacity was denied before any paid step",
          MockEmbed.call_count == 0)
check("the capped item's ocr_status is 'failed', not stuck 'running' forever",
      refresh_item("ocr_cap1").ocr_status == "failed")

# ─────────────────────────────────────────────────────────────────────────
section("Wall-clock expiry reconciliation — injected clock, never sleeps")
# ─────────────────────────────────────────────────────────────────────────
# A trial user: is_premium=False, premium_until=None the WHOLE time — the
# entitlement TOKEN never changes across trial end, so only the
# free_lock_last_effective_premium snapshot can catch this transition.
trial_id = "wc_trial"
u_trial = User(id=trial_id, email="wc@example.com", created_at=NOW)  # inside the trial right now
db.add(u_trial); db.commit()
mkitem("wc1", trial_id, last_active_at=NOW)
mkitem("wc2", trial_id, last_active_at=NOW - datetime.timedelta(hours=1))
mkitem("wc3", trial_id, last_active_at=NOW - datetime.timedelta(hours=2))
mkitem("wc4", trial_id, last_active_at=NOW - datetime.timedelta(hours=3))
ent.reconcile_free_lock_state(db, refresh_user(trial_id))
check("while inside the trial, reconcile is a no-op (dormant) — every source stays selectable",
      db.query(LibraryItem).filter(LibraryItem.user_id == trial_id,
                                    LibraryItem.is_unlocked_selection.is_(True)).count() == 0)
check("the entitled snapshot was recorded", refresh_user(trial_id).free_lock_last_effective_premium is True)

# Inject the clock forward past the 7-day trial WITHOUT touching is_premium
# or premium_until at all — created_at is the only thing that "moves".
db.query(User).filter(User.id == trial_id).update(
    {"created_at": NOW - datetime.timedelta(days=8)})
db.commit()
u_after = refresh_user(trial_id)
check("effective_premium is now False purely from the clock — is_premium/premium_until untouched",
      not u_after.effective_premium and u_after.is_premium is False and u_after.premium_until is None)
token_before = u_after.free_lock_state_token
ent.reconcile_free_lock_state(db, u_after)
sel = {i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == trial_id, LibraryItem.is_unlocked_selection.is_(True))}
check("a PURE wall-clock trial expiry (no field write) is detected and finalizes the lock selection",
      sel == {"wc1", "wc2", "wc3"}, sel)
check("extras (wc4) become inactive immediately", refresh_item("wc4").is_active is False)
still_active = {i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == trial_id, LibraryItem.is_active.is_(True))}
check("no locked source remains in the active lineup", "wc4" not in still_active)

ent.reconcile_free_lock_state(db, refresh_user(trial_id))
check("a second reconcile call in the same expired episode is a true no-op (nothing rotates)",
      {i.id for i in db.query(LibraryItem).filter(
          LibraryItem.user_id == trial_id, LibraryItem.is_unlocked_selection.is_(True))} == {"wc1", "wc2", "wc3"})

# A real paid subscription expiring purely by wall clock (premium_until is a
# FIXED past timestamp the whole time — it never "changes" at the moment of
# expiry either).
paid_id = "wc_paid"
future_end = NOW + datetime.timedelta(seconds=1)  # about to expire, not yet
u_paid = mkuser(paid_id, premium_until=future_end, successful_sources_total=0)
mkitem("wcp1", paid_id); mkitem("wcp2", paid_id)
ent.reconcile_free_lock_state(db, refresh_user(paid_id))
check("still entitled a moment before premium_until: dormant, snapshot True",
      refresh_user(paid_id).free_lock_last_effective_premium is True)
# Inject the clock past premium_until without writing to premium_until itself.
db.query(User).filter(User.id == paid_id).update(
    {"premium_until": NOW - datetime.timedelta(minutes=1)})  # simulates "time passed" for the test
db.commit()
# NOTE: this DOES change premium_until's stored value, which is realistic —
# unlike the trial case, a real subscription's premium_until is a fixed
# value that does not itself move; what's being proven here is that
# reconcile fires on this transition regardless of WHICH signal (token or
# snapshot) catches it — see the trial case above for the token-invariant
# case specifically.
ent.reconcile_free_lock_state(db, refresh_user(paid_id))
check("paid-subscription wall-clock expiry finalizes correctly",
      {i.id for i in db.query(LibraryItem).filter(
          LibraryItem.user_id == paid_id, LibraryItem.is_unlocked_selection.is_(True))} == {"wcp1", "wcp2"})

section("Resubscription after wall-clock expiry, then a SECOND downgrade")
db.query(User).filter(User.id == trial_id).update(
    {"is_premium": True})
db.commit()
ent.reconcile_free_lock_state(db, refresh_user(trial_id))
check("resubscribing (is_premium=True) snapshots entitled again",
      refresh_user(trial_id).free_lock_last_effective_premium is True)
check("wc4 (previously locked) reads unlocked immediately on resubscription — no reconcile needed to unlock",
      ent.is_source_unlocked(refresh_user(trial_id), refresh_item("wc4")))
db.query(User).filter(User.id == trial_id).update({"is_premium": False})
db.commit()
ent.reconcile_free_lock_state(db, refresh_user(trial_id))
check("the SECOND downgrade re-finalizes correctly (same 3 remain valid, still eligible)",
      {i.id for i in db.query(LibraryItem).filter(
          LibraryItem.user_id == trial_id, LibraryItem.is_unlocked_selection.is_(True))} == {"wc1", "wc2", "wc3"})

# ─────────────────────────────────────────────────────────────────────────
section("Exactly three at initial downgrade — retained-source count table (0/1/2/3/4+)")
# ─────────────────────────────────────────────────────────────────────────
for n, expect_len in [(0, 0), (1, 1), (2, 2), (3, 3), (5, 3)]:
    uid = f"retain_{n}"
    uu = mkuser(uid, premium_until=NOW + datetime.timedelta(days=5))
    for k in range(n):
        mkitem(f"{uid}_i{k}", uid)
    db.query(User).filter(User.id == uid).update({"premium_until": OLD})
    db.commit()
    ent.reconcile_free_lock_state(db, refresh_user(uid))
    got = db.query(LibraryItem).filter(
        LibraryItem.user_id == uid, LibraryItem.is_unlocked_selection.is_(True)).count()
    check(f"{n} retained source(s) at downgrade → exactly {expect_len} unlocked", got == expect_len, got)

# ─────────────────────────────────────────────────────────────────────────
section("GET /bites/daily excludes a locked source's session")
# ─────────────────────────────────────────────────────────────────────────
u_daily = mkuser("daily_locked", premium_until=OLD, successful_sources_total=0)
mkitem("daily_lk1", "daily_locked", is_active=False)
mkitem("daily_lk2", "daily_locked", is_active=False)
mkitem("daily_lk3", "daily_locked", is_active=False)
mkitem("daily_lk4", "daily_locked", is_active=False)
ent.reconcile_free_lock_state(db, refresh_user("daily_locked"))
locked_daily = next(i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == "daily_locked", LibraryItem.is_unlocked_selection.is_(False)))
unlocked_daily = next(i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == "daily_locked", LibraryItem.is_unlocked_selection.is_(True)))
today = datetime.date.today()
db.add(DailyBite(id="daily_bite_locked", user_id="daily_locked", library_item_id=locked_daily,
                  title="L", insight="i", reflection="r", action="a", date=today,
                  cards=[{"t": 1}], read_at=None))
db.add(DailyBite(id="daily_bite_unlocked", user_id="daily_locked", library_item_id=unlocked_daily,
                  title="U", insight="i", reflection="r", action="a", date=today,
                  cards=[{"t": 1}], read_at=None))
db.commit()
as_user("daily_locked")
r = c.get("/bites/daily")
ids = {s["id"] for s in r.json()}
check("GET /bites/daily excludes the locked source's session", "daily_bite_locked" not in ids, ids)
check("GET /bites/daily still returns the unlocked source's session", "daily_bite_unlocked" in ids, ids)

# ─────────────────────────────────────────────────────────────────────────
section("Scheduler unread-hold excludes a locked source (no permanent deadlock)")
# ─────────────────────────────────────────────────────────────────────────
from app.services.notification_service import _live_unread_query

u_hold = mkuser("hold_locked", premium_until=OLD, successful_sources_total=0)
mkitem("hold_lk1", "hold_locked", is_active=False)
mkitem("hold_lk2", "hold_locked", is_active=False)
mkitem("hold_lk3", "hold_locked", is_active=False)
mkitem("hold_lk4", "hold_locked", is_active=False)
ent.reconcile_free_lock_state(db, refresh_user("hold_locked"))
locked_hold = next(i.id for i in db.query(LibraryItem).filter(
    LibraryItem.user_id == "hold_locked", LibraryItem.is_unlocked_selection.is_(False)))
db.add(DailyBite(id="hold_bite", user_id="hold_locked", library_item_id=locked_hold,
                  title="H", insight="i", reflection="r", action="a",
                  date=today - datetime.timedelta(days=3), read_at=None))
db.commit()
unread = _live_unread_query(db, refresh_user("hold_locked"))
check("an unread bite on a source that's since become LOCKED does not hold the scheduler hostage "
      "(the same 'unreachable orphan' failure mode as a deleted book, now closed for locked sources too)",
      len(unread) == 0, [b.id for b in unread])

# ─────────────────────────────────────────────────────────────────────────
section("Migration/backfill fail-closed accounting")
# ─────────────────────────────────────────────────────────────────────────
import app.database as dbmod

with mock.patch("app.services.entitlement_service.backfill_existing_accounts", return_value=(3, 2)):
    raised = False
    try:
        dbmod._run_task2_required_migrations_and_backfill()
    except RuntimeError as e:
        raised = True
        msg = str(e)
    check("a backfill pass reporting ANY failures raises — required migration/backfill fails closed",
          raised and "2" in msg, msg if raised else "did not raise")

with mock.patch("app.services.entitlement_service.backfill_existing_accounts", return_value=(5, 0)):
    raised2 = False
    try:
        dbmod._run_task2_required_migrations_and_backfill()
    except RuntimeError:
        raised2 = True
    check("a fully-successful backfill pass does NOT raise", not raised2)

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Task 2 remediation checks passed")
sys.exit(1 if failures else 0)
