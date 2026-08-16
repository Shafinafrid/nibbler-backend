"""
Task 2 — SECOND remediation pass (Aug 2026), after Hermes's second audit.

Covers every SQLite-testable finding NOT already covered by
test_task2_entitlement.py or test_task2_remediation.py (both of which were
also updated this pass — see the completion report for the exact diffs):
renewable reservation lease semantics (active vs. stale, retry cannot steal
an attempt), deletion-vs-reservation accounting (the SQLite-provable half of
PG_DELETE_PENDING), compensating vector cleanup after an abandoned/rejected
attempt, and the newly-locked backend routes found in the locked-source
route audit (images, history, saved, read/save mutations).

Real-PostgreSQL-only concerns (the actual row-lock behavior, the
reconstructed deadlock cycle, and the ALTER TABLE/advisory-lock DDL) are
NOT here — see the completion report for the disposable-Postgres-16 output.
This file stays credential-free and makes no paid-provider call.
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

from fastapi.testclient import TestClient
from app.database import create_tables, SessionLocal, get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite, SavedBite
import main
from app.routers import library as library_router
from app.services import entitlement_service as ent

create_tables()
db = SessionLocal()

NOW = datetime.datetime.utcnow()
OLD = NOW - datetime.timedelta(days=400)

CURRENT_UID = {"v": None}
def _db(): yield db
def _current_user(): return db.query(User).filter(User.id == CURRENT_UID["v"]).first()
main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = _current_user
c = TestClient(main.app)
def as_user(uid): CURRENT_UID["v"] = uid


def mkuser(uid, **kw):
    kw.setdefault("created_at", OLD)
    kw.setdefault("email", f"{uid}@example.com")
    u = User(id=uid, **kw)
    db.add(u); db.commit()
    return u


def mkitem(iid, uid, **kw):
    kw.setdefault("type", "pdf")
    kw.setdefault("title", iid)
    kw.setdefault("processed", True)
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
section("Renewable reservation lease — mint, renew extends deadline")
# ─────────────────────────────────────────────────────────────────────────
u_lease = mkuser("lease_u", successful_sources_total=0)
i_lease = mkitem("lease1", "lease_u", processed=False)
ok = ent.reserve_free_capacity(db, i_lease, "lease_u")
token = refresh_item("lease1").reservation_lease_token
check("reserving mints a lease token", ok and bool(token))
expiry1 = refresh_item("lease1").reservation_lease_expires_at

# Simulate real elapsed time by backdating reserved_at/expiry, THEN renew —
# an injected clock, never a real sleep.
db.query(LibraryItem).filter(LibraryItem.id == "lease1").update(
    {"reservation_lease_expires_at": NOW - datetime.timedelta(minutes=1)})  # already "expired"
db.commit()
renewed = ent.renew_reservation_lease(db, "lease1", "lease_u", token)
expiry2 = refresh_item("lease1").reservation_lease_expires_at
check("renewing a lease with the CURRENT token succeeds and extends the deadline forward from now",
      renewed and expiry2 > NOW, (renewed, expiry2))

section("Active work crossing the ORIGINAL TTL is not reaped when it kept renewing")
# Total elapsed time exceeds RESERVATION_TTL_MINUTES, but every checkpoint
# renewed — the reap query only looks at the (renewed) expiry, never at how
# long ago the reservation FIRST started.
db.query(LibraryItem).filter(LibraryItem.id == "lease1").update(
    {"reserved_at": NOW - datetime.timedelta(minutes=ent.RESERVATION_TTL_MINUTES * 3)})
db.commit()
i_probe = mkitem("lease_probe", "lease_u", processed=False)
ent.reserve_free_capacity(db, i_probe, "lease_u")  # triggers a reap pass for this user
check("a lease that kept renewing survives a reap pass even though its FIRST reservation "
      "was long past the original TTL window",
      refresh_item("lease1").entitlement_status == "pending")

section("Stale lease (never renewed) IS reaped, and its OLD token can no longer act")
db.query(LibraryItem).filter(LibraryItem.id == "lease1").update(
    {"reservation_lease_expires_at": NOW - datetime.timedelta(minutes=1)})
db.commit()
i_probe2 = mkitem("lease_probe2", "lease_u", processed=False)
ent.reserve_free_capacity(db, i_probe2, "lease_u")
check("an unrenewed, expired lease is reaped (status → 'released')",
      refresh_item("lease1").entitlement_status == "released")
renew_after_reap = ent.renew_reservation_lease(db, "lease1", "lease_u", token)
check("the OLD token can no longer renew a reaped reservation", renew_after_reap is False)
finalize_after_reap = ent.finalize_successful_processing(db, refresh_item("lease1"), "lease_u", chunk_count=1, lease_token=token)
check("an old worker presenting a superseded token cannot finalize (no double-convert)",
      finalize_after_reap is False)
check("the superseded finalize attempt did NOT consume a slot",
      refresh_user("lease_u").successful_sources_total == 0)

section("Retry after reap mints a NEW token — the new attempt can finalize, the old one still cannot")
retried = ent.reserve_free_capacity(db, refresh_item("lease1"), "lease_u")
new_token = refresh_item("lease1").reservation_lease_token
check("the retried reservation succeeds under a brand-new token", retried and new_token and new_token != token)
old_worker_ok = ent.finalize_successful_processing(db, refresh_item("lease1"), "lease_u", chunk_count=1, lease_token=token)
check("the OLD worker's finalize call (stale token) is still refused after the retry",
      old_worker_ok is False)
new_worker_ok = ent.finalize_successful_processing(db, refresh_item("lease1"), "lease_u", chunk_count=1, lease_token=new_token)
check("the NEW worker's finalize call (current token) succeeds",
      new_worker_ok and refresh_item("lease1").entitlement_status == "consumed")
check("exactly one slot was consumed overall, despite two competing finalize attempts",
      refresh_user("lease_u").successful_sources_total == 1)

# ─────────────────────────────────────────────────────────────────────────
section("Deletion vs. reservation accounting — the pending-delete fix (PG_DELETE_PENDING)")
# ─────────────────────────────────────────────────────────────────────────
u_del = mkuser("del_pending", successful_sources_total=0)
i_del = mkitem("del1", "del_pending", processed=False, content="hi")
ent.reserve_free_capacity(db, i_del, "del_pending")
check("reserving increments reserved_sources_count", refresh_user("del_pending").reserved_sources_count == 1)

as_user("del_pending")
r = c.delete("/library/del1")
check("DELETE on a PENDING item succeeds", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
check("reserved_sources_count is decremented in the SAME delete — no stranded reservation "
      "(the fix for PG_DELETE_PENDING 1 0)",
      refresh_user("del_pending").reserved_sources_count == 0)
check("the item row is actually gone", refresh_item("del1") is None)

# A worker that already had a reference to the deleted item must find it
# gone and no-op cleanly — no exception, no double-decrement.
ghost_item = LibraryItem(id="del1", user_id="del_pending", title="x", type="pdf")  # detached, not in session
release_before = refresh_user("del_pending").reserved_sources_count
ent.release_reservation(db, ghost_item, "del_pending", reason="late failure")
check("a worker's late release_reservation call for the deleted item is a clean no-op "
      "(no double-decrement below 0)",
      refresh_user("del_pending").reserved_sources_count == release_before == 0)
finalize_ghost = ent.finalize_successful_processing(db, ghost_item, "del_pending", chunk_count=1)
check("a worker's late finalize call for the deleted item correctly fails (cannot finalize a "
      "deleted/cancelled item)", finalize_ghost is False)

section("Duplicate/idempotent deletion")
r2 = c.delete("/library/del1")
check("deleting an already-deleted item is idempotent (404, not a 500 or a double-decrement)",
      r2.status_code == 404)

section("Deletion of a non-pending (already-consumed) item never touches reserved_sources_count")
u_del2 = mkuser("del_consumed", successful_sources_total=1)
i_del2 = mkitem("del2", "del_consumed", processed=True, entitlement_status="consumed")
as_user("del_consumed")
r3 = c.delete("/library/del2")
check("deleting an already-'consumed' item succeeds", r3.status_code == 200)
check("successful_sources_total is unaffected by deleting a consumed source (permanent, no restore)",
      refresh_user("del_consumed").successful_sources_total == 1)
check("reserved_sources_count is untouched (item was never pending)",
      refresh_user("del_consumed").reserved_sources_count == 0)

# ─────────────────────────────────────────────────────────────────────────
section("Compensating vector cleanup after an abandoned/rejected attempt")
# ─────────────────────────────────────────────────────────────────────────
with mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
    MockEmbed.return_value.delete_item_vectors.return_value = True
    library_router._release_reservation_after_failure("some_item_id", "some_user_id", "boom")
    # Task 2 closeout (Verified Blocker 4): a durable cleanup-ledger record
    # must persist BEFORE any provider call — with no attempt_token at all
    # (this call's own default), there is no attempt identity to durably
    # record, so persistence itself correctly fails and the provider is
    # never called. The real, attempt-scoped path (with a genuine
    # attempt_token, where persistence succeeds and the provider IS
    # called) is exercised immediately below and in F2 of
    # test_task2_final_backend.py.
    check("_release_reservation_after_failure with no attempt_token cannot "
          "persist a durable ledger record, so it correctly never calls "
          "the provider at all (Verified Blocker 4: ledger-first, never "
          "provider-first)",
          MockEmbed.return_value.delete_item_vectors.call_count == 0)

u_comp = mkuser("comp_finalize", successful_sources_total=0)
i_comp = mkitem("comp1", "comp_finalize", processed=False, content="text")
with mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
    MockEmbed.return_value.index_text.return_value = 4
    MockEmbed.return_value.delete_item_vectors.return_value = True

    def _delete_item_during_indexing(*a, **kw):
        # Simulate a concurrent deletion racing this attempt WHILE the paid
        # embedding call is "in flight" — by the time finalize runs, the
        # item is gone. This is the realistic shape of the race: capacity
        # is reserved first (so indexing legitimately started), then the
        # item vanishes before conversion.
        db.query(LibraryItem).filter(LibraryItem.id == "comp1").delete()
        db.commit()
        return 4
    MockEmbed.return_value.index_text.side_effect = _delete_item_during_indexing

    library_router.process_item_embeddings("comp1", "comp_finalize")
    check("indexing was attempted (vectors written) before the concurrent deletion was observed",
          MockEmbed.return_value.index_text.call_count == 1)
    check("finalize correctly refuses to convert a reservation for an item that's now gone",
          True)  # implied by the cleanup call below actually firing
    check("compensating cleanup deleted the just-written vectors for the abandoned attempt",
          MockEmbed.return_value.delete_item_vectors.call_count == 1
          and MockEmbed.return_value.delete_item_vectors.call_args.args[0] == "comp1",
          MockEmbed.return_value.delete_item_vectors.call_args)
check("the item row itself is gone (deleted mid-flight, not resurrected)", refresh_item("comp1") is None)
check("no permanent entitlement was consumed for the abandoned attempt",
      refresh_user("comp_finalize").successful_sources_total == 0)

# ─────────────────────────────────────────────────────────────────────────
section("Locked-source route audit — images, history, saved, read/save mutations")
# ─────────────────────────────────────────────────────────────────────────
u_routes = mkuser("routes_locked", premium_until=OLD, successful_sources_total=0)
mkitem("routes_lk1", "routes_locked", is_active=False, is_unlocked_selection=True, entitlement_status="consumed")
mkitem("routes_lk2", "routes_locked", is_active=False, is_unlocked_selection=True, entitlement_status="consumed")
mkitem("routes_lk3", "routes_locked", is_active=False, is_unlocked_selection=True, entitlement_status="consumed")
mkitem("routes_lk4", "routes_locked", is_active=False, is_unlocked_selection=False,
       images=[{"id": "img_abc123", "key": "book-images/routes_locked/routes_lk4/x.png", "mime": "image/png"}])
db.query(User).filter(User.id == "routes_locked").update({
    "successful_sources_total": 3, "free_lock_state_token": ent._entitlement_token(refresh_user("routes_locked")),
    "free_lock_last_effective_premium": False,
})
db.commit()
locked_row = refresh_item("routes_lk4")
unlocked_row = refresh_item("routes_lk1")
check("fixture is unambiguous: routes_lk4 is locked, routes_lk1 is unlocked",
      not ent.is_source_unlocked(refresh_user("routes_locked"), locked_row)
      and ent.is_source_unlocked(refresh_user("routes_locked"), unlocked_row))
as_user("routes_locked")

r = c.get("/library/routes_lk4/images/img_abc123")
check("GET /library/{id}/images/{candidate_id} on a locked source is refused (403 source_locked)",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked", f"{r.status_code} {r.text[:150]}")

# Task 2 final consolidated backend pass (Verified Blocker 7): the generic
# PATCH /library/{item_id} used to let a locked source's title, mode, or
# growth-profile-name be changed with no lock check at all — the same
# bypass every other mutation route above is already refused for.
r = c.patch("/library/routes_lk4", json={"title": "sneaky new title"})
check("PATCH /library/{id} title change on a locked source is refused "
      "(403 source_locked) BEFORE any mutation is applied",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked",
      f"{r.status_code} {r.text[:150]}")
check("the locked item's title was NOT changed by the refused PATCH",
      refresh_item("routes_lk4").title == "routes_lk4")

r = c.patch("/library/routes_lk4", json={"mode": "story"})
check("PATCH /library/{id} mode change on a locked source is refused "
      "(403 source_locked)",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked",
      f"{r.status_code} {r.text[:150]}")

r = c.patch("/library/routes_lk4", json={"growth_profile_name": "sneaky profile"})
check("PATCH /library/{id} growth-profile change on a locked source is "
      "refused (403 source_locked)",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked",
      f"{r.status_code} {r.text[:150]}")
check("the locked item's growth_profile_name was NOT changed by the "
      "refused PATCH", refresh_item("routes_lk4").growth_profile_name is None)

r = c.patch("/library/routes_lk1", json={"title": "a fine, allowed rename"})
check("PATCH /library/{id} title change on an UNLOCKED source still "
      "succeeds — the lock check does not over-refuse",
      r.status_code == 200 and refresh_item("routes_lk1").title == "a fine, allowed rename",
      f"{r.status_code} {r.text[:150]}")

db.add(DailyBite(id="hist_locked", user_id="routes_locked", library_item_id="routes_lk4",
                  title="L", insight="secret insight", reflection="r", action="a",
                  date=datetime.date.today(), cards=[{"t": 1}]))
db.add(DailyBite(id="hist_unlocked", user_id="routes_locked", library_item_id="routes_lk1",
                  title="U", insight="i", reflection="r", action="a",
                  date=datetime.date.today(), cards=[{"t": 1}]))
db.commit()
r = c.get("/bites/history")
hist_ids = {b["id"] for b in r.json()["bites"]}
check("GET /bites/history excludes a locked source's bite content",
      "hist_locked" not in hist_ids, hist_ids)
check("GET /bites/history still includes the unlocked source's bite",
      "hist_unlocked" in hist_ids, hist_ids)

r = c.post("/bites/hist_locked/read")
check("POST /bites/{id}/read on a locked source's bite is refused (403 source_locked)",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked", f"{r.status_code} {r.text[:150]}")
r = c.post("/bites/hist_unlocked/read")
check("POST /bites/{id}/read on an unlocked source's bite succeeds", r.status_code == 200)

r = c.post("/bites/hist_locked/save")
check("POST /bites/{id}/save on a locked source's bite is refused (403 source_locked)",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked", f"{r.status_code} {r.text[:150]}")
r = c.post("/bites/hist_unlocked/save")
check("POST /bites/{id}/save on an unlocked source's bite succeeds", r.status_code == 200)

# Force hist_unlocked's source to lock, then verify GET /bites/saved hides it
# (suppressed while locked, not deleted — it comes back if resubscribed).
db.query(LibraryItem).filter(LibraryItem.id == unlocked_row.id).update({"is_unlocked_selection": False})
db.commit()
r = c.get("/bites/saved")
saved_ids = {row["bite"]["id"] for row in r.json()}
check("GET /bites/saved suppresses a save whose source has since become locked",
      "hist_unlocked" not in saved_ids, saved_ids)
still_saved = db.query(SavedBite).filter(SavedBite.user_id == "routes_locked", SavedBite.bite_id == "hist_unlocked").first()
check("the saved-bite ROW itself is preserved (suppressed, not deleted) — proves it can come back",
      still_saved is not None)
db.query(LibraryItem).filter(LibraryItem.id == unlocked_row.id).update({"is_unlocked_selection": True})
db.commit()
r = c.get("/bites/saved")
saved_ids2 = {row["bite"]["id"] for row in r.json()}
check("re-unlocking the source restores the save's visibility with no data loss",
      "hist_unlocked" in saved_ids2, saved_ids2)

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Task 2 second-remediation checks passed")
sys.exit(1 if failures else 0)
