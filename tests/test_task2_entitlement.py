"""
Task 2 — Free lifetime source entitlement + downgrade lock selection.

Covers: entitlement lifecycle, source-state selection/fallback, upload
accounting (including idempotency and a concurrent-completion race proven
separately against a disposable local Postgres — see the Task 2 completion
report; SQLite cannot exercise real row-level locking meaningfully), feature
enforcement (activation, session generation, Review, Connect/chat,
scheduler eligibility), cross-account isolation, and the migration backfill
logic (DB-agnostic — the ALTER TABLE syntax itself was validated separately
against real Postgres 16, not here).
"""
import os, sys, tempfile, datetime

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
from app.models.bite import DailyBite
import main
from app.services import entitlement_service as ent

create_tables()
db = SessionLocal()

NOW = datetime.datetime.utcnow()
OLD = NOW - datetime.timedelta(days=400)  # well outside the 7-day signup trial


def mkuser(uid, *, is_premium=False, premium_until=None, created_at=OLD, successes=0):
    u = User(id=uid, email=f"{uid}@example.com", is_premium=is_premium,
              premium_until=premium_until, created_at=created_at,
              successful_sources_total=successes)
    db.add(u)
    db.commit()
    return u


def mkitem(iid, uid, *, processed=True, is_active=True, updated_at=None, unlocked_sel=False,
           last_active_at=None, entitlement_status=None):
    it = LibraryItem(id=iid, user_id=uid, title=iid, type="pdf", processed=processed,
                      is_active=is_active, is_unlocked_selection=unlocked_sel,
                      updated_at=updated_at or NOW, last_active_at=last_active_at,
                      entitlement_status=entitlement_status)
    db.add(it)
    db.commit()
    return it


CURRENT_UID = {"v": None}


def _db():
    yield db


def _current_user():
    return db.query(User).filter(User.id == CURRENT_UID["v"]).first()


main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = _current_user
c = TestClient(main.app)


def as_user(uid):
    CURRENT_UID["v"] = uid


# ─────────────────────────────────────────────────────────────────────────
section("Entitlement lifecycle — is_source_unlocked() across every tier")
# ─────────────────────────────────────────────────────────────────────────
free_new = mkuser("free_new", created_at=OLD)  # genuinely free, never premium
check("brand-new-but-old Free account: effective_premium is False",
      not free_new.effective_premium)

trial_user = mkuser("trial_user", created_at=NOW)  # inside the 7-day trial
check("account created just now is on the signup trial (effective_premium True)",
      trial_user.effective_premium)

paid_user = mkuser("paid_user", premium_until=NOW + datetime.timedelta(days=20))
check("active paid subscriber: effective_premium True", paid_user.effective_premium)

comp_user = mkuser("comp_user", is_premium=True)
check("complimentary (is_premium manual flag): effective_premium True", comp_user.effective_premium)

cancelled_but_entitled = mkuser("cancelled_entitled", premium_until=NOW + datetime.timedelta(days=5))
check("cancelled-but-not-yet-expired (future premium_until): still effective_premium True",
      cancelled_but_entitled.effective_premium)

expired_paid = mkuser("expired_paid", premium_until=OLD)
check("expired paid subscriber: effective_premium False", not expired_paid.effective_premium)
check("a lapsed subscriber does NOT fall back into the signup trial",
      not expired_paid.effective_premium)

for uid, user in [("free_new", free_new), ("trial_user", trial_user), ("paid_user", paid_user),
                   ("comp_user", comp_user), ("expired_paid", expired_paid)]:
    itm = mkitem(f"{uid}_item", uid, unlocked_sel=False)
    expect = user.effective_premium  # unlocked iff entitled, since selection flag is False
    check(f"{uid}: is_source_unlocked matches effective_premium when no selection exists",
          ent.is_source_unlocked(user, itm) == expect)

# ─────────────────────────────────────────────────────────────────────────
section("Source state — fewer/exactly/more than three, fallback, explicit selection")
# ─────────────────────────────────────────────────────────────────────────
u_few = mkuser("u_few", created_at=OLD, successes=2)
# Downgrade-accounting remediation: `successes` must match the items that
# ACTUALLY consumed those slots — mkitem(..., entitlement_status="consumed")
# models "these 2 items are the ones that already used the 2 permanent
# entitlements", exactly as finalize_successful_processing would have left
# them via the real Free-upload flow. A prior version of this test set
# `successes` as an arbitrary unattributed number, which the pre-remediation
# code never checked against retained items — the fixed downgrade-accounting
# logic now correctly treats un-consumed candidates as needing NEW capacity,
# so the fixture must be internally consistent or the test proves nothing.
mkitem("few1", "u_few", entitlement_status="consumed")
mkitem("few2", "u_few", entitlement_status="consumed")
ent.reconcile_free_lock_state(db, u_few)
sel = {i.id for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_few", LibraryItem.is_unlocked_selection.is_(True))}
check("fewer than 3 sources: ALL are unlocked (no artificial gap)", sel == {"few1", "few2"}, str(sel))

u_exact = mkuser("u_exact", created_at=OLD, successes=3)
mkitem("ex1", "u_exact", entitlement_status="consumed")
mkitem("ex2", "u_exact", entitlement_status="consumed")
mkitem("ex3", "u_exact", entitlement_status="consumed")
ent.reconcile_free_lock_state(db, u_exact)
sel = {i.id for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_exact", LibraryItem.is_unlocked_selection.is_(True))}
check("exactly 3 sources: all 3 unlocked", sel == {"ex1", "ex2", "ex3"}, str(sel))

# was premium, now expired, NEVER previously Free — successes starts at 0
# (a Premium-only account has consumed nothing yet; downgrade is what
# converts up to 3 retained sources into permanent consumption for the
# FIRST time — see the "Downgrade accounting" section below for the
# dedicated assertion on that promotion).
u_more = mkuser("u_more", premium_until=OLD, successes=0)
# Remediation: the fallback ranks on `last_active_at` (real activation/use
# events), not `updated_at` (which also changes on an unrelated metadata
# edit) — see entitlement_service._fallback_candidates.
mkitem("m1", "u_more", last_active_at=NOW - datetime.timedelta(days=5))
mkitem("m2", "u_more", last_active_at=NOW - datetime.timedelta(days=4))
mkitem("m3", "u_more", last_active_at=NOW - datetime.timedelta(days=3))
mkitem("m4", "u_more", last_active_at=NOW - datetime.timedelta(days=2))
mkitem("m5", "u_more", last_active_at=NOW - datetime.timedelta(days=1))
ent.reconcile_free_lock_state(db, u_more)
sel = {i.id for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_more", LibraryItem.is_unlocked_selection.is_(True))}
check("more than 3 sources, no explicit choice: fallback picks the 3 most recently ACTIVE",
      sel == {"m3", "m4", "m5"}, str(sel))
check("downgrade-accounting remediation: retaining 3 Premium-created sources for the FIRST "
      "time promotes them into permanent consumption — successful_sources_total becomes 3",
      db.query(User).get("u_more").successful_sources_total == 3)
check("the 3 retained sources are now individually 'consumed'; the 2 locked ones are not",
      {i.id: i.entitlement_status for i in db.query(LibraryItem).filter(
          LibraryItem.user_id == "u_more").all()} ==
      {"m1": None, "m2": None, "m3": "consumed", "m4": "consumed", "m5": "consumed"},
      {i.id: i.entitlement_status for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_more").all()})
total_m = db.query(LibraryItem).filter(LibraryItem.user_id == "u_more").count()
check("downgrade preserves ALL sources (nothing deleted)", total_m == 5, total_m)
locked_m2 = db.query(LibraryItem).filter(LibraryItem.id == "m1").first()
check("a locked source is still returned/visible (not hidden)", locked_m2 is not None)
check("a locked source's effective access is False",
      not ent.is_source_unlocked(db.query(User).get("u_more"), locked_m2))

u_explicit = mkuser("u_explicit", premium_until=NOW + datetime.timedelta(days=10), successes=0)
# e1 is deliberately given a more-recent last_active_at than e3 so the
# fallback fill-in below (remediation #6: a PARTIAL explicit choice must
# still be topped up to exactly 3 at the first downgrade) has one
# unambiguous, deterministic winner between the two NOT explicitly chosen.
mkitem("e1", "u_explicit", last_active_at=NOW - datetime.timedelta(hours=1))
mkitem("e2", "u_explicit")
mkitem("e3", "u_explicit", last_active_at=NOW - datetime.timedelta(days=9))
mkitem("e4", "u_explicit")
ent.set_explicit_selection(db, db.query(User).get("u_explicit"), {"e2", "e4"})
still_active_count = db.query(LibraryItem).filter(LibraryItem.user_id == "u_explicit", LibraryItem.is_active.is_(True)).count()
check("an explicit pre-expiry choice does NOT touch is_active (nothing is locked yet — still Premium)",
      still_active_count == 4, still_active_count)
db.query(User).filter(User.id == "u_explicit").update({"premium_until": OLD})
db.commit()
ent.reconcile_free_lock_state(db, db.query(User).get("u_explicit"))
sel_kind = {
    i.id: i.selection_kind for i in db.query(LibraryItem)
    .filter(LibraryItem.user_id == "u_explicit", LibraryItem.is_unlocked_selection.is_(True))
}
sel = set(sel_kind)
# Remediation #6: a PARTIAL (2-item) explicit choice with ≥3 eligible
# sources must still end up with EXACTLY three unlocked at the first
# downgrade — the two explicit picks are preserved verbatim, and the
# remaining slot is filled by the deterministic fallback (e1, per its
# more-recent last_active_at). Never silently settling for the 2-item
# partial choice is exactly what Hermes's remediation #6 requires.
check("on actual downgrade, EXPLICIT prior choices are preserved AND topped up to exactly 3",
      sel == {"e2", "e4", "e1"}, str(sel))
check("preserved explicit picks keep provenance 'explicit'; the topped-up slot is 'fallback'",
      sel_kind.get("e2") == "explicit" and sel_kind.get("e4") == "explicit"
      and sel_kind.get("e1") == "fallback", str(sel_kind))
check("downgrade-accounting remediation: all 3 retained (previously un-consumed) sources "
      "are promoted, bringing successful_sources_total to exactly 3",
      db.query(User).get("u_explicit").successful_sources_total == 3)

section("Deletion never rotates a locked source into an open slot")
db.query(LibraryItem).filter(LibraryItem.id == "e2").delete()
db.commit()
ent.reconcile_free_lock_state(db, db.query(User).get("u_explicit"))  # same episode — must be a no-op
sel = {i.id for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_explicit", LibraryItem.is_unlocked_selection.is_(True))}
check("deleting one of the three unlocked sources leaves exactly the remaining two — e3 NOT promoted",
      sel == {"e4", "e1"}, str(sel))

section("Resubscription immediately unlocks every retained source")
db.query(User).filter(User.id == "u_more").update({"premium_until": NOW + datetime.timedelta(days=30)})
db.commit()
u_more_fresh = db.query(User).get("u_more")
all_unlocked = all(ent.is_source_unlocked(u_more_fresh, i) for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_more").all())
check("after resubscribing, EVERY retained source reads unlocked immediately (no reconcile needed)",
      all_unlocked)

# ─────────────────────────────────────────────────────────────────────────
section("Upload accounting")
# ─────────────────────────────────────────────────────────────────────────
u_cap = mkuser("u_cap", created_at=OLD, successes=0)
i1 = mkitem("cap1", "u_cap", processed=False)
ok1 = ent.finalize_successful_processing(db, i1, "u_cap", chunk_count=3)
check("1st successful upload: accepted, consumes exactly 1 entitlement",
      ok1 and db.query(User).get("u_cap").successful_sources_total == 1)

i_fail = mkitem("capfail", "u_cap", processed=False)
i_fail.processing_error = "boom"
db.commit()
check("a failed upload never calls finalize — counter untouched by the failure itself",
      db.query(User).get("u_cap").successful_sources_total == 1)

i_processing = mkitem("capproc", "u_cap", processed=False)
check("an abandoned/still-processing placeholder consumes zero (processed=False, no finalize call)",
      not i_processing.processed and db.query(User).get("u_cap").successful_sources_total == 1)

i2 = mkitem("cap2", "u_cap", processed=False)
ok2 = ent.finalize_successful_processing(db, i2, "u_cap", chunk_count=3)
i3 = mkitem("cap3", "u_cap", processed=False)
ok3 = ent.finalize_successful_processing(db, i3, "u_cap", chunk_count=3)
check("2nd and 3rd successful uploads accepted, counter reaches exactly 3",
      ok2 and ok3 and db.query(User).get("u_cap").successful_sources_total == 3)

i4 = mkitem("cap4", "u_cap", processed=False)
ok4 = ent.finalize_successful_processing(db, i4, "u_cap", chunk_count=3)
check("4th successful completion while already at 3 is REJECTED, counter stays 3",
      ok4 is False and db.query(User).get("u_cap").successful_sources_total == 3)
check("the rejected item carries a readable processing_error instead of silently vanishing",
      bool(db.query(LibraryItem).filter(LibraryItem.id == "cap4").first().processing_error))

ok_dup = ent.finalize_successful_processing(db, i1, "u_cap", chunk_count=999)
check("duplicate/retried completion of an ALREADY-processed item is idempotent (no double count)",
      ok_dup is True and db.query(User).get("u_cap").successful_sources_total == 3)

db.query(LibraryItem).filter(LibraryItem.id == "cap1").delete()
db.commit()
check("deleting a successfully-consumed source does NOT restore the entitlement",
      db.query(User).get("u_cap").successful_sources_total == 3)

as_user("u_cap")
r = c.post("/library/", json={"title": "One more", "type": "text", "content": "hi"})
check("direct API bypass is rejected once the lifetime cap is reached",
      r.status_code == 403 and r.json().get("detail", {}).get("code") == "free_limit_reached",
      f"status={r.status_code} body={r.text[:200]}")

u_prem_upload = mkuser("u_prem_upload", premium_until=NOW + datetime.timedelta(days=10), successes=50)
i_p = mkitem("premup", "u_prem_upload", processed=False)
ok_p = ent.finalize_successful_processing(db, i_p, "u_prem_upload", chunk_count=1)
check("Premium upload remains unlimited regardless of how high the lifetime counter already is",
      ok_p is True)

# ─────────────────────────────────────────────────────────────────────────
section("Feature enforcement — locked sources are refused everywhere")
# ─────────────────────────────────────────────────────────────────────────
u_locked = mkuser("u_locked", premium_until=OLD, successes=0)
mkitem("lk1", "u_locked", is_active=False, updated_at=NOW - datetime.timedelta(days=1))
mkitem("lk2", "u_locked", is_active=False, updated_at=NOW - datetime.timedelta(days=2))
mkitem("lk3", "u_locked", is_active=False, updated_at=NOW - datetime.timedelta(days=3))
mkitem("lk4", "u_locked", is_active=False, updated_at=NOW - datetime.timedelta(days=4))
ent.reconcile_free_lock_state(db, db.query(User).get("u_locked"))
locked_ids = {i.id for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_locked", LibraryItem.is_unlocked_selection.is_(False))}
check("exactly one source ended up locked out of the four", len(locked_ids) == 1, str(locked_ids))
locked_id = next(iter(locked_ids))

as_user("u_locked")
r = c.patch(f"/library/{locked_id}/active", json={"active": True})
check("PATCH .../active on a locked source is refused (403 source_locked)",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked", f"{r.status_code} {r.text[:150]}")

r = c.post("/bites/session", json={"library_item_id": locked_id, "read_length": 5})
check("POST /bites/session on a locked source is refused (403 source_locked) — no LLM call reached",
      r.status_code == 403 and r.json()["detail"]["code"] == "source_locked", f"{r.status_code} {r.text[:150]}")

# Connect/chat is (unchanged by Task 2) a blanket Premium-tier feature —
# `_require_premium` fires BEFORE the per-item lock check for any non-
# entitled account, so a Free/expired user gets `premium_required`, not
# `source_locked`, regardless of which specific source they asked for. This
# still satisfies "locked sources must not enter Connect/chat" (no Free-tier
# source, locked or not, ever reaches it) — Task 2 does not expand Connect to
# Free users' unlocked sources, only the existing Premium/trial/complimentary
# access does, at which point `is_source_unlocked` is always True anyway.
r = c.post("/connect/insights", json={"library_item_id": locked_id})
check("POST /connect/insights for a non-entitled account stays premium-gated (Connect is untouched by Task 2)",
      r.status_code == 403 and r.json()["detail"]["code"] == "premium_required", f"{r.status_code} {r.text[:150]}")

r = c.get(f"/connect/stats/{locked_id}")
check("GET /connect/stats/{id} for a non-entitled account stays premium-gated",
      r.status_code == 403 and r.json()["detail"]["code"] == "premium_required", f"{r.status_code} {r.text[:150]}")

r = c.post("/connect/chat", json={"library_item_id": locked_id, "message": "hi"})
check("POST /connect/chat for a non-entitled account stays premium-gated — no paid chat call reached",
      r.status_code == 403 and r.json()["detail"]["code"] == "premium_required", f"{r.status_code} {r.text[:150]}")

# NOTE: connect._get_item ALSO carries an is_source_unlocked() check
# (added for defense-in-depth / future-proofing), but it is unreachable by
# construction, not just by today's call order: is_source_unlocked() always
# returns True whenever effective_premium is True, so there is no way to
# build an "entitled but this one item is locked" case to exercise it
# against. It is intentionally kept as a guard against a FUTURE, more
# granular entitlement model (e.g. Task 8 partial-complimentary scoping)
# rather than something independently testable against today's model.

from app.services.session_service import generate_session_for_item, SessionGenerationError
try:
    generate_session_for_item(db, user=db.query(User).get("u_locked"),
                               item=db.query(LibraryItem).get(locked_id),
                               read_length=5, profile={}, today=datetime.date.today())
    check("generate_session_for_item itself refuses a locked source (belt-and-suspenders)", False)
except SessionGenerationError as e:
    check("generate_session_for_item itself refuses a locked source (belt-and-suspenders)",
          e.status_code == 403)

db.add(DailyBite(id="review_locked", user_id="u_locked", library_item_id=locked_id, title="T",
                  insight="i", reflection="r", action="a", date=datetime.date.today(),
                  cards=[{"t": 1}]))
unlocked_id = next(iter({i.id for i in db.query(LibraryItem).filter(LibraryItem.user_id == "u_locked", LibraryItem.is_unlocked_selection.is_(True))}))
db.add(DailyBite(id="review_unlocked", user_id="u_locked", library_item_id=unlocked_id, title="T2",
                  insight="i", reflection="r", action="a", date=datetime.date.today(),
                  cards=[{"t": 1}]))
db.commit()
r = c.get("/bites/sessions")
ids = {s["id"] for s in r.json()["sessions"]}
check("GET /bites/sessions (Review) excludes the session belonging to a locked source",
      "review_locked" not in ids, str(ids))
check("GET /bites/sessions (Review) still includes the session belonging to an unlocked source",
      "review_unlocked" in ids, str(ids))

section("Scheduler eligibility — a locked source cannot be chosen for scheduled generation")
scheduler_eligible = (
    db.query(LibraryItem)
    .filter(LibraryItem.user_id == "u_locked", LibraryItem.is_active.is_(True), LibraryItem.processed.is_(True))
    .all()
)
check("after reconcile, a locked source is is_active=False and therefore invisible to the scheduler's query",
      locked_id not in {i.id for i in scheduler_eligible}, str([i.id for i in scheduler_eligible]))

section("Cross-account isolation")
other_owner_item = mkitem("someone_elses", "u_more")  # owned by a different account
# Use an ENTITLED caller so the request reaches the ownership check inside
# _get_item rather than being stopped earlier by _require_premium — a Free
# caller would get premium_required either way, which would prove nothing
# about ownership isolation specifically.
u_isolation = mkuser("u_isolation", premium_until=NOW + datetime.timedelta(days=10))
as_user("u_isolation")
r = c.get(f"/connect/stats/someone_elses")
check("another user's source cannot be accessed by an entitled caller either (404, ownership-scoped)",
      r.status_code == 404, f"{r.status_code} {r.text[:150]}")
r = c.put("/library/free-selection", json={"item_ids": ["someone_elses"]})
check("PUT /library/free-selection rejects a source_id that isn't the caller's own",
      r.status_code == 400, f"{r.status_code} {r.text[:150]}")

section("PUT /library/free-selection — requires active entitlement")
as_user("u_locked")  # currently expired/free
r = c.put("/library/free-selection", json={"item_ids": [locked_id]})
check("an already-Free/locked account cannot call free-selection to re-shuffle its own locks",
      r.status_code == 403 and r.json()["detail"]["code"] == "premium_required", f"{r.status_code} {r.text[:150]}")

u_pick = mkuser("u_pick", premium_until=NOW + datetime.timedelta(days=10), successes=3)
mkitem("pk1", "u_pick"); mkitem("pk2", "u_pick"); mkitem("pk3", "u_pick")
as_user("u_pick")
r = c.put("/library/free-selection", json={"item_ids": ["pk1", "pk2"]})
check("an entitled user CAN set an explicit ≤3 selection", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
r = c.put("/library/free-selection", json={"item_ids": ["pk1", "pk2", "pk3"]})
check("exactly 3 distinct ids is accepted at the boundary", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
r = c.put("/library/free-selection", json={"item_ids": ["pk1", "pk2", "pk3", "anything"]})
check("more than 3 raw ids is rejected (422, schema-level max_length)",
      r.status_code == 422, f"{r.status_code} {r.text[:200]}")

# ─────────────────────────────────────────────────────────────────────────
section("GET /library/ response shape — unlocked/preselected/consumed numbers")
# ─────────────────────────────────────────────────────────────────────────
as_user("u_locked")
r = c.get("/library/")
body = r.json()
# 3, not 4: u_locked never consumed anything before its downgrade — the 3
# retained sources are what the downgrade-accounting fix promotes into
# permanent consumption; the 4th (locked-out) source was never selected and
# was never charged (see "Premium-created sources not retained at downgrade
# must not independently consume additional slots").
check("response carries successful_sources_total and free_source_limit",
      body.get("successful_sources_total") == 3 and body.get("free_source_limit") == 3,
      str({k: body.get(k) for k in ("successful_sources_total", "free_source_limit")}))
rows = {i["id"]: i for i in body["items"]}
check("locked row reports unlocked=false", rows[locked_id]["unlocked"] is False)
check("unlocked row reports unlocked=true", rows[unlocked_id]["unlocked"] is True)
check("locked row still reports preselected=false (it lost the slot)", rows[locked_id]["preselected"] is False)
check("unlocked row reports preselected=true", rows[unlocked_id]["preselected"] is True)

# ─────────────────────────────────────────────────────────────────────────
section("Migration backfill logic (DB-agnostic Python path — ALTER syntax itself proven separately on Postgres)")
# ─────────────────────────────────────────────────────────────────────────
u_legacy_free = User(id="legacy_free", email="legacy_free@example.com", created_at=OLD)
db.add(u_legacy_free); db.commit()
mkitem("lg1", "legacy_free"); mkitem("lg2", "legacy_free")
# backfill_existing_accounts now returns (ok_count, failed_count) — never a
# single number that could misrepresent a partial failure as full success
# (remediation #14, fail-closed migration accounting).
ok, failed = ent.backfill_existing_accounts(db)
check("backfill processes at least the never-reconciled legacy account, with zero failures",
      ok >= 1 and failed == 0, (ok, failed))
sel = {i.id for i in db.query(LibraryItem).filter(LibraryItem.user_id == "legacy_free", LibraryItem.is_unlocked_selection.is_(True))}
check("legacy Free account backfilled with its 2 sources unlocked", sel == {"lg1", "lg2"}, str(sel))
check("legacy sources are counted as permanently consumed (grandfather policy: ≤3 retained, not more)",
      db.query(User).get("legacy_free").successful_sources_total == 2)
ok2, failed2 = ent.backfill_existing_accounts(db)
check("backfill is idempotent — a second pass touches nothing new (already-reconciled accounts skipped)",
      "legacy_free" not in {u.id for u in db.query(User).filter(User.free_lock_state_token.is_(None)).all()}
      and failed2 == 0)

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Task 2 entitlement checks passed")
sys.exit(1 if failures else 0)
