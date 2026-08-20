"""
Task 15 — Ensure failed uploads never consume a Free source entitlement.

Investigation (Aug 2026) found the core concern already fully solved by the
Task 2 remediation passes: `successful_sources_total` (the one PERMANENT
counter `can_accept_new_upload` reads) is only ever incremented by
`finalize_successful_processing` on a verified success — a failed/rejected/
abandoned upload only ever touches the TEMPORARY `reserved_sources_count`
via `release_reservation`, which is wired into every processing-pipeline
failure branch. That is exhaustively covered by test_task2_entitlement.py,
test_task2_remediation.py/2/3, and test_task2_attempt_lifecycle_repro.py.

Two small, genuinely new gaps this file closes:

  A-F — `_reap_stale_reservations` only ever ran LAZILY (triggered by that
        same account's own next reservation attempt). A user whose upload
        worker crashed and who never uploads again would have that item
        sit "processing" forever. `reap_all_stale_reservations` is a new
        global scheduled sweep (wired into the same 5-minute maintenance
        cycle as the other Task 2 autonomous passes) that closes this —
        operational/UX only, since a stuck 'pending' row was already
        correctly excluded from the permanent counter either way.
  G     — the genuinely new END-TO-END integration scenario the audit
        asked for explicitly, stitching reserve → fail → retry → succeed
        into one story (the individual pieces were already tested
        separately, but never as one continuous narrative).
"""
import os, sys, tempfile, datetime, unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task15.db", FIREBASE_PROJECT_ID="t")
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
from app.services import entitlement_service as ent

create_tables()
db = SessionLocal()

NOW = datetime.datetime.utcnow()
OLD = NOW - datetime.timedelta(days=400)


def mkuser(uid, **kw):
    kw.setdefault("created_at", OLD)
    kw.setdefault("email", f"{uid}@example.com")
    u = User(id=uid, **kw)
    db.add(u); db.commit()
    return u


def mkitem(iid, uid, **kw):
    kw.setdefault("type", "pdf")
    kw.setdefault("title", iid)
    kw.setdefault("processed", False)
    it = LibraryItem(id=iid, user_id=uid, **kw)
    db.add(it); db.commit()
    return it


def refresh_user(uid):
    db.expire_all()
    return db.query(User).filter(User.id == uid).first()


def refresh_item(iid):
    db.expire_all()
    return db.query(LibraryItem).filter(LibraryItem.id == iid).first()


# ═══════════════════════════════════════════════════════════════════════
section("A — the global sweep reaps a genuinely stale reservation for a user who never uploads again")
# ═══════════════════════════════════════════════════════════════════════
u_a = mkuser("sweep_a", successful_sources_total=1)
i_a = mkitem("sweep_a1", "sweep_a")
ent.reserve_free_capacity(db, i_a, "sweep_a")
old_ts = NOW - datetime.timedelta(minutes=ent.RESERVATION_TTL_MINUTES + 5)
db.query(LibraryItem).filter(LibraryItem.id == "sweep_a1").update({"reservation_lease_expires_at": old_ts})
db.commit()

# The crucial difference from the pre-existing lazy-reap tests: NOTHING
# calls reserve_free_capacity for "sweep_a" again. Only the global sweep
# can possibly reclaim this — proving the actual gap this closes.
users_reaped, items_reaped = ent.reap_all_stale_reservations(db)
check("the sweep reaped exactly one user, one item",
      users_reaped == 1 and items_reaped == 1, (users_reaped, items_reaped))
check("the stale item is now 'released', not left 'pending' forever",
      refresh_item("sweep_a1").entitlement_status == "released")
check("reserved_sources_count is decremented back to 0",
      refresh_user("sweep_a").reserved_sources_count == 0)
check("successful_sources_total is COMPLETELY UNTOUCHED — a stuck reservation was "
      "never counted toward the permanent limit, and reaping it must not change "
      "that in either direction",
      refresh_user("sweep_a").successful_sources_total == 1)


# ═══════════════════════════════════════════════════════════════════════
section("B — a reservation still within its lease window is NOT touched by the sweep")
# ═══════════════════════════════════════════════════════════════════════
u_b = mkuser("sweep_b")
i_b = mkitem("sweep_b1", "sweep_b")
ent.reserve_free_capacity(db, i_b, "sweep_b")
db.query(LibraryItem).filter(LibraryItem.id == "sweep_b1").update(
    {"reservation_lease_expires_at": NOW + datetime.timedelta(minutes=25)})
db.commit()

users_reaped, items_reaped = ent.reap_all_stale_reservations(db)
check("a genuinely active reservation is not among the candidates the sweep touches",
      refresh_item("sweep_b1").entitlement_status == "pending")
check("reserved_sources_count for that account is untouched",
      refresh_user("sweep_b").reserved_sources_count == 1)


# ═══════════════════════════════════════════════════════════════════════
section("C — a legacy 'pending' row with no lease_expires_at is reaped via the sweep too "
        "(the reserved_at fallback, not just the per-account lazy path)")
# ═══════════════════════════════════════════════════════════════════════
u_c = mkuser("sweep_c")
legacy = mkitem("sweep_c1", "sweep_c", entitlement_status="pending",
                 reserved_at=NOW - datetime.timedelta(minutes=ent.RESERVATION_TTL_MINUTES + 5),
                 reservation_lease_expires_at=None)
db.query(User).filter(User.id == "sweep_c").update({"reserved_sources_count": 1})
db.commit()

ent.reap_all_stale_reservations(db)
check("a pre-lease-remediation legacy row (no lease_expires_at) still reaps via the "
      "reserved_at fallback through the GLOBAL sweep entrypoint",
      refresh_item("sweep_c1").entitlement_status == "released")


# ═══════════════════════════════════════════════════════════════════════
section("D — one sweep call processes MULTIPLE different users' stale reservations independently")
# ═══════════════════════════════════════════════════════════════════════
for uid in ("sweep_d1", "sweep_d2", "sweep_d3"):
    mkuser(uid)
    mkitem(f"{uid}_item", uid)
    ent.reserve_free_capacity(db, refresh_item(f"{uid}_item"), uid)
    db.query(LibraryItem).filter(LibraryItem.id == f"{uid}_item").update(
        {"reservation_lease_expires_at": old_ts})
db.commit()

users_reaped, items_reaped = ent.reap_all_stale_reservations(db)
check("all three independently-stale users were reaped in one sweep call",
      users_reaped == 3 and items_reaped == 3, (users_reaped, items_reaped))
for uid in ("sweep_d1", "sweep_d2", "sweep_d3"):
    check(f"{uid}'s reservation is released", refresh_item(f"{uid}_item").entitlement_status == "released")


# ═══════════════════════════════════════════════════════════════════════
section("E — a renewal landing exactly during the sweep still survives (TOCTOU safety carries "
        "through the new global entrypoint, not just the old per-account path)")
# ═══════════════════════════════════════════════════════════════════════
u_e = mkuser("sweep_e")
i_e = mkitem("sweep_e1", "sweep_e")
ent.reserve_free_capacity(db, i_e, "sweep_e")
tok_e = refresh_item("sweep_e1").reservation_lease_token
db.query(LibraryItem).filter(LibraryItem.id == "sweep_e1").update({"reservation_lease_expires_at": old_ts})
db.commit()

import app.services.entitlement_service as ent_mod
_orig_reap = ent_mod._reap_stale_reservations
def _reap_with_renewal_race(db_, user_):
    # Simulate a heartbeat landing in the window between the sweep's
    # unlocked candidate scan and the per-user lock actually being granted.
    ent.renew_reservation_lease(db_, "sweep_e1", "sweep_e", tok_e)
    return _orig_reap(db_, user_)

with mock.patch.object(ent_mod, "_reap_stale_reservations", side_effect=_reap_with_renewal_race):
    ent.reap_all_stale_reservations(db)
check("an item renewed in the window right before the sweep's lock is acquired survives, "
      "even reached through the NEW global sweep rather than the old per-account path",
      refresh_item("sweep_e1").entitlement_status == "pending")


# ═══════════════════════════════════════════════════════════════════════
section("F — an exception reaping one user does not block the rest of the batch")
# ═══════════════════════════════════════════════════════════════════════
u_f1 = mkuser("sweep_f1")
mkitem("sweep_f1_item", "sweep_f1")
ent.reserve_free_capacity(db, refresh_item("sweep_f1_item"), "sweep_f1")
db.query(LibraryItem).filter(LibraryItem.id == "sweep_f1_item").update({"reservation_lease_expires_at": old_ts})
u_f2 = mkuser("sweep_f2")
mkitem("sweep_f2_item", "sweep_f2")
ent.reserve_free_capacity(db, refresh_item("sweep_f2_item"), "sweep_f2")
db.query(LibraryItem).filter(LibraryItem.id == "sweep_f2_item").update({"reservation_lease_expires_at": old_ts})
db.commit()

_real_reap_for_f = ent_mod._reap_stale_reservations
def _reap_raise_for_f1(db_, user_):
    if user_.id == "sweep_f1":
        raise RuntimeError("simulated failure reaping sweep_f1")
    return _real_reap_for_f(db_, user_)

with mock.patch.object(ent_mod, "_reap_stale_reservations", side_effect=_reap_raise_for_f1):
    users_reaped, items_reaped = ent.reap_all_stale_reservations(db)
check("sweep_f1's failure is isolated — sweep_f2 still gets reaped in the same call",
      refresh_item("sweep_f2_item").entitlement_status == "released")
check("the batch correctly reports only the successful reap, not the failed one",
      users_reaped == 1 and items_reaped == 1, (users_reaped, items_reaped))
# sweep_f1 itself: the exception path rolls back, so its item is left exactly as it
# was (still 'pending') — not corrupted, not falsely reaped, not double-released.
check("sweep_f1's own item is untouched by the failed attempt (rolled back cleanly, "
      "not left in a half-mutated state)",
      refresh_item("sweep_f1_item").entitlement_status == "pending")


# ═══════════════════════════════════════════════════════════════════════
section("G — END-TO-END: reserve → fail → immediately retry → succeed, as one continuous story")
# ═══════════════════════════════════════════════════════════════════════
u_g = mkuser("story_u", successful_sources_total=2)   # already at 2/3 permanent slots
check("starting state: 2/3 consumed, one Free slot genuinely available",
      ent.can_accept_new_upload(refresh_user("story_u")))

# Attempt 1: reserves the 3rd slot, then processing fails.
attempt1 = mkitem("story_attempt1", "story_u")
ok1 = ent.reserve_free_capacity(db, attempt1, "story_u")
check("attempt 1 is admitted (2 consumed + 0 reserved < limit 3)", ok1)
check("while attempt 1 is pending, a genuinely fresh upload probe is correctly BLOCKED "
      "(all remaining capacity is temporarily reserved, not paywalled outright)",
      not ent.reserve_free_capacity(db, mkitem("story_blocked_probe", "story_u"), "story_u"))
ent.release_reservation(db, refresh_item("story_attempt1"), "story_u", reason="processing failed")
check("after the failure, successful_sources_total is STILL exactly 2 — the failed "
      "attempt consumed nothing permanent",
      refresh_user("story_u").successful_sources_total == 2)
check("the failed attempt's row is 'released', not silently stuck 'pending' forever",
      refresh_item("story_attempt1").entitlement_status == "released")

# The user immediately retries — a brand-new item, same logical 3rd slot.
attempt2 = mkitem("story_attempt2", "story_u")
ok2 = ent.reserve_free_capacity(db, attempt2, "story_u")
check("the retry is admitted — the failed attempt's slot is genuinely available again, "
      "not permanently lost", ok2)

# This time it succeeds.
ent.finalize_successful_processing(db, refresh_item("story_attempt2"), "story_u", chunk_count=5,
                                    lease_token=refresh_item("story_attempt2").reservation_lease_token)
final_user = refresh_user("story_u")
check("the retry's success consumes EXACTLY ONE entitlement — 2 → 3, not 2 → 4 for "
      "two attempts", final_user.successful_sources_total == 3)
check("the account is now correctly at the permanent 3/3 cap",
      not ent.can_accept_new_upload(final_user))
check("attempt2's row is 'consumed', not left 'pending'",
      refresh_item("story_attempt2").entitlement_status == "consumed")

# The failed attempt1 row is still sitting there (visible in Library, honestly
# labelled) — removing it must not perturb the now-permanent count either way.
# (Full removal-path coverage already lives in test_task13_library_deletion_
# safety.py; this just confirms the entitlement side of that story finishes
# cleanly, closing the loop this section opened.)
check("the discarded failed attempt's row never became 'consumed' at any point "
      "in this story", refresh_item("story_attempt1").entitlement_status == "released")

# Finally: the global sweep must be a complete no-op against this whole story —
# nothing here is stale (attempt2 is 'consumed', attempt1 is already 'released').
before = (final_user.successful_sources_total, final_user.reserved_sources_count)
ent.reap_all_stale_reservations(db)
after_user = refresh_user("story_u")
check("running the sweep afterward changes nothing about this account's entitlement state",
      (after_user.successful_sources_total, after_user.reserved_sources_count) == before,
      (before, (after_user.successful_sources_total, after_user.reserved_sources_count)))


print("\n" + "=" * 62)
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: all Task 15 checks passed")
sys.exit(0)
