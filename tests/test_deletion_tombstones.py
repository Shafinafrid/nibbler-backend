"""
Finding #7 (deferred, "durable profile backup") — deletion tombstones for the
growth-profile multi-profile feature.

The bug (reproduced against the real PUT /profile/growth comparison logic
before this fix): whole-blob last-writer-wins by `updatedAt` means a STALE
device — one that never learned a profile was deleted elsewhere — can push a
later timestamp that still carries the deleted profile in its own profiles[]
and win the LWW compare, resurrecting a profile the user explicitly deleted
on another device. Wall-clock order between two independent edits proves
nothing about which one reflects the deletion.

Fix: a server-side, never-shrinking UNION tombstone set
(profiles.deleted_profile_ids), enforced on every PUT /profile/growth against
the INCOMING profiles[] itself — before the existing 409 "don't overwrite
non-empty with empty" guard and before the LWW timestamp compare — so even a
push that would otherwise "win" cannot reintroduce a tombstoned profile.
GET /profile/ filters against the same set as defense in depth.

Covers, through the REAL HTTP stack (TestClient, exactly as the app calls
it):
  1. The exact resurrection scenario: device A deletes P2 and pushes; device
     B (stale, unaware) pushes a later, unrelated edit that still contains
     P2 — P2 must not reappear in the stored OR read-back state.
  2. A push that never mentions deletedProfileIds at all (older app build)
     still works normally — no crash, no unintended tombstoning.
  3. Two different profiles deleted from two different devices — both
     tombstoned (union behavior), neither un-deletes the other.
  4. The existing "refuse non-empty-to-empty overwrite" 409 guard still
     fires correctly AFTER tombstone filtering — verified in both directions:
     it must still fire when real (non-tombstoned) profiles would be wiped,
     and it must NOT fire when the incoming "empty" push is empty only
     because every profile it dropped was already tombstoned.
  5. GET /profile/ never shows a tombstoned profile, even via a stale
     intermediate read (defense-in-depth filter, independent of the PUT path).

    .venv/bin/python tests/test_deletion_tombstones.py
"""

import os
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/tombstones.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app.database import create_tables, SessionLocal, get_db  # noqa: E402
from app.middleware.auth import get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.profile import Profile  # noqa: E402
import main  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


def section(t):
    print(f"\n=== {t} ===")


create_tables()
db = SessionLocal()

ACTIVE_USER = {"id": None}
main.app.dependency_overrides[get_db] = lambda: db
main.app.dependency_overrides[get_current_user] = \
    lambda: db.query(User).filter(User.id == ACTIVE_USER["id"]).first()
client = TestClient(main.app)


def mkuser(uid):
    db.add(User(id=uid, email=f"{uid}@example.com"))
    db.commit()
    ACTIVE_USER["id"] = uid
    return uid


def push_growth(profiles, active_id, updated_at, deleted_ids=None, person_name="Person"):
    body = {
        "growth_state": {
            "person": {"name": person_name},
            "profiles": profiles,
            "activeProfileId": active_id,
            "updatedAt": updated_at,
        },
    }
    if deleted_ids is not None:
        body["deletedProfileIds"] = deleted_ids
    return client.put("/profile/growth", json=body)


def get_profile_json():
    return client.get("/profile/").json()


def profile_ids(gs_json):
    return sorted(p["id"] for p in (gs_json.get("growth_state") or {}).get("profiles", []))


P1 = {"id": "p1", "name": "Reading"}
P2 = {"id": "p2", "name": "Fitness"}
P3 = {"id": "p3", "name": "Cooking"}

# ═════════════════════════════════════════════════════════════════════════
section("1 — exact resurrection scenario: stale device can't undo a deletion")
# ═════════════════════════════════════════════════════════════════════════
mkuser("resurrection-user")

# Seed: both devices start with [P1, P2].
r0 = push_growth([P1, P2], "p1", "2026-09-01T10:00:00.000Z", deleted_ids=[])
check("seed push OK", r0.status_code == 200, r0.text)
check("seed has both profiles", profile_ids(r0.json()) == ["p1", "p2"], profile_ids(r0.json()))

# Device A deletes P2, pushes with a tombstone and a fresh (earlier) timestamp.
rA = push_growth([P1], "p1", "2026-09-01T11:00:00.000Z", deleted_ids=["p2"])
check("A's delete push OK", rA.status_code == 200, rA.text)
check("A's stored state has only P1", profile_ids(rA.json()) == ["p1"], profile_ids(rA.json()))
check(
    "A's response echoes the tombstone",
    rA.json().get("deletedProfileIds") == ["p2"],
    rA.json().get("deletedProfileIds"),
)

# Device B is stale — never saw the deletion, still has [P1, P2] locally.
# B renames P1 and pushes ITS OWN full blob, timestamped LATER than A's push
# (a completely plausible real-world case: B's edit genuinely happened after
# A's deletion in wall-clock time, it just didn't know about it).
rB = push_growth(
    [{"id": "p1", "name": "Reading (renamed)"}, P2], "p1",
    "2026-09-01T12:00:00.000Z", deleted_ids=[],
)
check("B's push OK (not rejected)", rB.status_code == 200, rB.text)
check(
    "P2 does NOT reappear despite B's later timestamp",
    "p2" not in profile_ids(rB.json()),
    profile_ids(rB.json()),
)
check("B's rename of P1 still applied", profile_ids(rB.json()) == ["p1"], profile_ids(rB.json()))
names = [p["name"] for p in rB.json()["growth_state"]["profiles"]]
check("P1's rename survived (only P2 was tombstoned)", names == ["Reading (renamed)"], names)

# Read-back must agree.
read_after = get_profile_json()
check(
    "GET /profile/ after the race also excludes P2",
    "p2" not in profile_ids(read_after),
    profile_ids(read_after),
)
check(
    "GET /profile/ reports p2 in deletedProfileIds",
    "p2" in read_after.get("deletedProfileIds", []),
    read_after.get("deletedProfileIds"),
)

# ═════════════════════════════════════════════════════════════════════════
section("2 — older app build (no deletedProfileIds field at all) still works")
# ═════════════════════════════════════════════════════════════════════════
mkuser("legacy-client-user")

r1 = client.put("/profile/growth", json={
    "growth_state": {
        "person": {"name": "Legacy"},
        "profiles": [P1, P2],
        "activeProfileId": "p1",
        "updatedAt": "2026-09-01T10:00:00.000Z",
    },
    # deliberately omitting "deletedProfileIds" entirely
})
check("legacy push (no field) succeeds", r1.status_code == 200, r1.text)
check("legacy push stores both profiles", profile_ids(r1.json()) == ["p1", "p2"], profile_ids(r1.json()))
check("legacy push has no tombstones", r1.json().get("deletedProfileIds", []) == [], r1.json().get("deletedProfileIds"))

# A second push, also without the field, still works and doesn't crash or
# spuriously tombstone anything.
r2 = client.put("/profile/growth", json={
    "growth_state": {
        "person": {"name": "Legacy"},
        "profiles": [P1, P2, P3],
        "activeProfileId": "p1",
        "updatedAt": "2026-09-01T11:00:00.000Z",
    },
})
check("second legacy push succeeds", r2.status_code == 200, r2.text)
check("second legacy push stores all three", profile_ids(r2.json()) == ["p1", "p2", "p3"], profile_ids(r2.json()))

# ═════════════════════════════════════════════════════════════════════════
section("3 — two different profiles deleted from two different devices (union)")
# ═════════════════════════════════════════════════════════════════════════
mkuser("union-user")

push_growth([P1, P2, P3], "p1", "2026-09-01T10:00:00.000Z", deleted_ids=[])

# Device A deletes P2.
rA3 = push_growth([P1, P3], "p1", "2026-09-01T11:00:00.000Z", deleted_ids=["p2"])
check("device A delete of p2 OK", rA3.status_code == 200, rA3.text)
check("after A: p2 gone, p1/p3 remain", profile_ids(rA3.json()) == ["p1", "p3"], profile_ids(rA3.json()))

# Device B (independently, unaware of A's deletion) deletes P3.
rB3 = push_growth([P1], "p1", "2026-09-01T12:00:00.000Z", deleted_ids=["p3"])
check("device B delete of p3 OK", rB3.status_code == 200, rB3.text)
check("after B: only p1 remains", profile_ids(rB3.json()) == ["p1"], profile_ids(rB3.json()))
check(
    "tombstone set is the UNION {p2, p3}, not just B's own p3",
    sorted(rB3.json().get("deletedProfileIds", [])) == ["p2", "p3"],
    rB3.json().get("deletedProfileIds"),
)

# A stale device C, unaware of EITHER deletion, pushes all three back —
# neither p2 nor p3 should resurrect.
rC3 = push_growth([P1, P2, P3], "p1", "2026-09-01T13:00:00.000Z", deleted_ids=[])
check("device C's push OK", rC3.status_code == 200, rC3.text)
check(
    "neither p2 nor p3 resurrected by C",
    profile_ids(rC3.json()) == ["p1"],
    profile_ids(rC3.json()),
)

# ═════════════════════════════════════════════════════════════════════════
section("4 — 409 guard still fires correctly AFTER tombstone filtering")
# ═════════════════════════════════════════════════════════════════════════
mkuser("guard-interaction-user")

push_growth([P1, P2], "p1", "2026-09-01T10:00:00.000Z", deleted_ids=[])

# 4a: a push that tries to wipe a REAL (non-tombstoned) profile with an empty
# list must still be rejected — the tombstone mechanism must not have
# weakened this guard.
r4a = push_growth([], "p1", "2026-09-01T11:00:00.000Z", deleted_ids=[])
check("4a: empty push over real profiles still 409s", r4a.status_code == 409, r4a.text)
# Confirm it was genuinely rejected — the server state is unchanged.
read_4a = get_profile_json()
check("4a: server state unchanged after the 409", profile_ids(read_4a) == ["p1", "p2"], profile_ids(read_4a))

# 4b: the interaction case — a push whose incoming profiles[] becomes empty
# ONLY because every profile in it was already tombstoned (not because the
# device is trying to wipe real data) must NOT trip the 409, since existing
# real profiles are compared post-filter too. Delete p1 for real first so the
# server's own state is just [p2], then push a stale blob containing ONLY p1
# (already tombstoned) with a deletion tombstone for p2 — the incoming list
# empties out entirely via tombstone filtering on BOTH sides, which must
# resolve as a legitimate (filtered) empty state, not a 409.
r4_del_p1 = push_growth([P2], "p1", "2026-09-01T12:00:00.000Z", deleted_ids=["p1"])
check("4b setup: p1 deleted for real", r4_del_p1.status_code == 200, r4_del_p1.text)
check("4b setup: server now has only p2", profile_ids(r4_del_p1.json()) == ["p2"], profile_ids(r4_del_p1.json()))

r4b = push_growth([P1], "p1", "2026-09-01T13:00:00.000Z", deleted_ids=["p2"])
check("4b: push whose only content is tombstoned data does NOT 409", r4b.status_code == 200, r4b.text)
check(
    "4b: both p1 and p2 end up tombstoned, profiles[] is legitimately empty",
    profile_ids(r4b.json()) == [],
    profile_ids(r4b.json()),
)
check(
    "4b: deletedProfileIds is the union {p1, p2}",
    sorted(r4b.json().get("deletedProfileIds", [])) == ["p1", "p2"],
    r4b.json().get("deletedProfileIds"),
)

# ═════════════════════════════════════════════════════════════════════════
section("5 — GET /profile/ never shows a tombstoned profile, even via a stale intermediate read")
# ═════════════════════════════════════════════════════════════════════════
mkuser("readback-user")

push_growth([P1, P2, P3], "p1", "2026-09-01T10:00:00.000Z", deleted_ids=[])

# Read before any deletion — sanity check all three are visible.
pre = get_profile_json()
check("5 pre-delete: all three visible via GET", profile_ids(pre) == ["p1", "p2", "p3"], profile_ids(pre))

# Delete p2, but via a push whose growth_state LOSES the LWW compare (older
# timestamp than what's stored) — this is the trickiest interaction: the
# tombstone must still land AND still be reflected on read, even though the
# growth_state body itself is correctly ignored as stale.
stale_delete = push_growth([P1, P3], "p1", "2026-09-01T09:00:00.000Z", deleted_ids=["p2"])
check("5: stale-timestamped delete push still succeeds (200, not rejected)", stale_delete.status_code == 200, stale_delete.text)
check(
    "5: response (even though growth_state body was stale) excludes p2",
    "p2" not in profile_ids(stale_delete.json()),
    profile_ids(stale_delete.json()),
)

# A direct GET (simulating "a stale intermediate read" / any other read path)
# must also never show p2 again.
post = get_profile_json()
check("5: GET /profile/ after a stale-LWW delete push excludes p2", "p2" not in profile_ids(post), profile_ids(post))
check("5: p2 present in deletedProfileIds on read", "p2" in post.get("deletedProfileIds", []), post.get("deletedProfileIds"))

# Defense-in-depth: simulate something writing growth_state AROUND the PUT
# path entirely (directly via the ORM, bypassing update_growth_state), still
# containing a tombstoned id — GET must filter it out anyway.
raw_profile = db.query(Profile).filter(Profile.user_id == "readback-user").first()
gs = dict(raw_profile.growth_state)
gs["profiles"] = [P1, P2, P3]  # smuggle p2 back in, bypassing the PUT-time filter
raw_profile.growth_state = gs
db.commit()

post_bypass = get_profile_json()
check(
    "5: defense-in-depth GET filter catches a write that bypassed PUT /profile/growth",
    "p2" not in profile_ids(post_bypass),
    profile_ids(post_bypass),
)

# ═════════════════════════════════════════════════════════════════════════
section("SUMMARY")
# ═════════════════════════════════════════════════════════════════════════
total = len(failures)
print(f"\n{'='*70}")
if total:
    print(f"FAILED: {total} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
