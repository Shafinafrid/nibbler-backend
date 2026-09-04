"""
Growth-profile assignment integrity + entitlement (Sep 2026).

Drives the REAL HTTP stack (TestClient) against the real routers, exactly as
the app calls them. Covers, in order:

  A. Collection merge   — absence != deletion; tombstones win and block
                          recreation; per-profile whole-body LWW on parsed
                          timestamps; ties keep the stored body; malformed/
                          missing timestamps can't overwrite; ledger,
                          ledgerBase and derived fields always come from the
                          SAME winning body; root-field rules; unknown future
                          root keys preserved.
  B. Canonical name     — a generic growth push carrying an OLD name cannot
                          revert a canonical rename, while the newer body's
                          unrelated ledger/pacing changes ARE still applied.
  C. Entitlement        — creation of additional profiles is Premium
                          everywhere: the canonical endpoint 403s, and the
                          generic PUT filters unentitled new ids while still
                          accepting valid edits/deletes in the same push.
  D. Assignment gating  — PATCH /library/{id} 403s for a non-entitled user;
                          create paths never fail, they substitute the
                          server default; explicit null resets to default;
                          story books keep both fields NULL.
  E. Integrity          — rename propagates to every assigned book in one
                          transaction; delete reassigns deterministically;
                          duplicate names are refused.
  F. Repair/promotion   — bootstrap rows (BOTH fields null) are attached;
                          unresolved-legacy rows (name set, no unique match)
                          are NOT touched; a legacy row becomes promoted once
                          its name is uniquely matchable.

    .venv/bin/python tests/test_growth_profile_assignment.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/gp_assign.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app.database import create_tables, SessionLocal, get_db  # noqa: E402
from app.middleware.auth import get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.profile import Profile  # noqa: E402
import main  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  — " + detail) if detail else ""))
    if not cond:
        failures.append(name)


def section(t):
    print("\n=== %s ===" % t)


create_tables()
db = SessionLocal()

ACTIVE = {"id": None}
main.app.dependency_overrides[get_db] = lambda: db
main.app.dependency_overrides[get_current_user] = \
    lambda: db.query(User).filter(User.id == ACTIVE["id"]).first()
client = TestClient(main.app)

T0 = "2026-09-01T10:00:00.000Z"
T1 = "2026-09-02T10:00:00.000Z"
T2 = "2026-09-03T10:00:00.000Z"


def mkuser(uid, premium=False):
    u = User(id=uid, email="%s@example.com" % uid)
    if premium:
        # A real, live subscription — effective_premium reads premium_until.
        u.premium_until = datetime.utcnow() + timedelta(days=30)
    else:
        # Explicitly LAPSED, not brand-new: a fresh user gets the 7-day
        # signup trial and would count as premium, masking the whole point.
        u.premium_until = datetime.utcnow() - timedelta(days=30)
    db.add(u)
    db.commit()
    ACTIVE["id"] = uid
    return uid


def set_premium(uid, premium):
    u = db.query(User).filter(User.id == uid).first()
    u.premium_until = datetime.utcnow() + (timedelta(days=30) if premium else timedelta(days=-30))
    db.commit()


def prof(pid, name, at=T1, **extra):
    d = {"id": pid, "name": name, "profileName": name, "updatedAt": at}
    d.update(extra)
    return d


def push(profiles, active=None, at=T1, deleted=None, person="P", extra_root=None):
    gs = {"person": {"name": person}, "profiles": profiles,
          "activeProfileId": active or (profiles[0]["id"] if profiles else None),
          "updatedAt": at}
    if extra_root:
        gs.update(extra_root)
    body = {"growth_state": gs}
    if deleted is not None:
        body["deletedProfileIds"] = deleted
    return client.put("/profile/growth", json=body)


def stored_state(uid):
    db.expire_all()
    p = db.query(Profile).filter(Profile.user_id == uid).first()
    return (p.growth_state or {}) if p else {}


def stored_ids(uid):
    return sorted(p["id"] for p in stored_state(uid).get("profiles", []))


def find_stored(uid, pid):
    for p in stored_state(uid).get("profiles", []):
        if p.get("id") == pid:
            return p
    return None


def mkitem(uid, item_id, *, gp_id=None, gp_name=None, mode="wisdom"):
    db.add(LibraryItem(id=item_id, user_id=uid, title=item_id, type="text",
                       mode=mode, processed=True,
                       growth_profile_id=gp_id, growth_profile_name=gp_name))
    db.commit()


def item(item_id):
    db.expire_all()
    return db.query(LibraryItem).filter(LibraryItem.id == item_id).first()


# ══════════════════════════════════════════════════════════════════════════
section("A. Collection merge — absence is not deletion")
u = mkuser("merge_user", premium=True)
push([prof("A", "Money"), prof("B", "Career")], active="A", at=T1)
check("both profiles stored", stored_ids(u) == ["A", "B"], str(stored_ids(u)))

# A stale device that never learned about B pushes only {A}, with a NEWER
# timestamp. Whole-blob replacement would destroy B here.
push([prof("A", "Money", at=T2)], active="A", at=T2)
check("stale push omitting B RETAINS B", stored_ids(u) == ["A", "B"], str(stored_ids(u)))

# Deletion still works — but only through an explicit tombstone.
push([prof("A", "Money", at=T2)], active="A", at=T2, deleted=["B"])
check("explicit tombstone deletes B", stored_ids(u) == ["A"], str(stored_ids(u)))

# ...and a tombstoned id can never come back.
push([prof("A", "Money", at=T2), prof("B", "Career", at=T2)], active="A", at=T2)
check("tombstoned B cannot be recreated", stored_ids(u) == ["A"], str(stored_ids(u)))

section("A2. Per-profile LWW keeps ONE internally consistent body")
u = mkuser("lww_user", premium=True)
push([prof("A", "Money", at=T1, ledger=["old"], ledgerBase={"v": 1}, pacing={"m": 5})], at=T1)
push([prof("A", "Money", at=T2, ledger=["old", "new"], ledgerBase={"v": 2}, pacing={"m": 10})], at=T2)
body = find_stored(u, "A")
check("newer body wins", body.get("ledger") == ["old", "new"], str(body.get("ledger")))
check("ledgerBase from the SAME winning body", body.get("ledgerBase") == {"v": 2})
check("pacing from the SAME winning body", body.get("pacing") == {"m": 10})

push([prof("A", "Money", at=T0, ledger=["stale"], ledgerBase={"v": 0})], at=T0)
body = find_stored(u, "A")
check("older body loses entirely", body.get("ledger") == ["old", "new"], str(body.get("ledger")))

# Exact tie -> the stored body wins, so a tie can never flip server state.
push([prof("A", "Money", at=T2, ledger=["tie-attempt"], ledgerBase={"v": 2})], at=T2)
check("exact timestamp tie keeps STORED body",
      find_stored(u, "A").get("ledger") == ["old", "new"])

# Malformed / missing timestamps must never displace a valid stored body.
push([{"id": "A", "name": "Money", "profileName": "Money",
       "updatedAt": "garbage", "ledger": ["bad"]}], at=T2)
check("malformed incoming timestamp cannot overwrite",
      find_stored(u, "A").get("ledger") == ["old", "new"])
push([{"id": "A", "name": "Money", "profileName": "Money", "ledger": ["none"]}], at=T2)
check("missing incoming timestamp cannot overwrite",
      find_stored(u, "A").get("ledger") == ["old", "new"])

section("A3. Timestamps are parsed, not string-compared")
u = mkuser("tz_user", premium=True)
# 11:00+02:00 == 09:00Z, which is EARLIER than 10:00Z — but as raw strings
# "2026-09-04T11:00..." sorts AFTER "2026-09-04T10:00...", so the old
# `str(a) < str(b)` compare accepted this stale push.
push([prof("A", "Money", at="2026-09-04T10:00:00+00:00", ledger=["kept"])],
     at="2026-09-04T10:00:00+00:00")
push([prof("A", "Money", at="2026-09-04T11:00:00+02:00", ledger=["stale"])],
     at="2026-09-04T11:00:00+02:00")
check("offset-aware compare rejects the earlier instant",
      find_stored(u, "A").get("ledger") == ["kept"],
      str(find_stored(u, "A").get("ledger")))

section("A4. Root fields")
u = mkuser("root_user", premium=True)
push([prof("A", "Money")], at=T1, person="Old", extra_root={"futureKey": {"keep": "me"}})
push([prof("A", "Money")], at=T2, person="New")
st = stored_state(u)
check("person = newer valid value", (st.get("person") or {}).get("name") == "New")
check("unknown future root key preserved", st.get("futureKey") == {"keep": "me"})
push([prof("A", "Money")], at=T0, person="Stale")
st = stored_state(u)
check("older root person ignored", (st.get("person") or {}).get("name") == "New")
check("root updatedAt keeps the max", st.get("updatedAt") == T2, str(st.get("updatedAt")))

section("A5. activeProfileId is repaired, never dangling")
u = mkuser("active_user", premium=True)
push([prof("A", "Money"), prof("B", "Career")], active="B", at=T1)
push([prof("A", "Money", at=T2)], active="B", at=T2, deleted=["B"])
check("activeProfileId repaired when its target is deleted",
      stored_state(u).get("activeProfileId") == "A",
      str(stored_state(u).get("activeProfileId")))

# ══════════════════════════════════════════════════════════════════════════
section("B. A stale push cannot revert a canonical rename")
u = mkuser("rename_user", premium=True)
push([prof("A", "Old Name", at=T1)], at=T1)
r = client.patch("/profile/profiles/A", json={"name": "New Name"})
check("canonical rename accepted", r.status_code == 200, str(r.status_code))
check("name is canonical after rename",
      find_stored(u, "A").get("profileName") == "New Name")

# Device B, still holding the OLD name, answers a question. Its body is
# genuinely newer, so it must win — WITHOUT reverting the rename.
push([prof("A", "Old Name", at=T2, ledger=["answer-from-stale-device"])], at=T2)
body = find_stored(u, "A")
check("canonical name SURVIVES the newer stale-named body",
      body.get("profileName") == "New Name", str(body.get("profileName")))
check("legacy `name` field also canonical", body.get("name") == "New Name")
check("the stale device's unrelated answer IS still applied",
      body.get("ledger") == ["answer-from-stale-device"], str(body.get("ledger")))

# ...but that protection is NARROW on purpose. A profile never renamed
# through the canonical endpoint keeps the long-standing behaviour: an
# ordinary offline rename carried in the blob still applies. With no
# canonical rename on record there is nothing for a stale device to revert
# TO, so defending it would only break the offline path that predates the
# endpoint (tests/test_deletion_tombstones.py relies on exactly that).
u = mkuser("blob_rename", premium=True)
push([prof("A", "Original", at=T1)], at=T1)
push([prof("A", "Renamed Offline", at=T2)], at=T2)
check("blob rename applies when NO canonical rename exists",
      find_stored(u, "A").get("profileName") == "Renamed Offline",
      str(find_stored(u, "A").get("profileName")))

# Bodies carrying no per-profile timestamp fall back to the ROOT one. Not
# every client stamps individual profiles; treating those as untimestamped
# would make every pair an exact tie and silently discard each edit.
u = mkuser("root_ts_only", premium=True)
client.put("/profile/growth", json={"growth_state": {
    "person": {"name": "P"}, "profiles": [{"id": "A", "name": "First"}],
    "activeProfileId": "A", "updatedAt": T1}})
client.put("/profile/growth", json={"growth_state": {
    "person": {"name": "P"}, "profiles": [{"id": "A", "name": "Second"}],
    "activeProfileId": "A", "updatedAt": T2}})
check("root timestamp breaks the tie when profiles are untimestamped",
      find_stored(u, "A").get("name") == "Second",
      str(find_stored(u, "A").get("name")))

# A wholly UNSTAMPED push (an old client sending no timestamps anywhere)
# must still apply: there is no staleness claim to weigh, and those builds
# depend on it. Only a DEMONSTRABLY older push is ignored.
u = mkuser("unstamped", premium=True)
client.put("/profile/growth", json={"growth_state": {
    "person": {"name": "First"}, "profiles": [{"id": "A", "name": "One"}],
    "activeProfileId": "A"}})
client.put("/profile/growth", json={"growth_state": {
    "person": {"name": "Second"}, "profiles": [{"id": "A", "name": "Two"}],
    "activeProfileId": "A"}})
check("unstamped push from an older client still applies",
      find_stored(u, "A").get("name") == "Two", str(find_stored(u, "A").get("name")))
check("...including its root person value",
      (stored_state(u).get("person") or {}).get("name") == "Second")

# But an unstamped push must NOT clobber a body the server has a real
# timestamp for — that IS a demonstrable staleness signal.
u = mkuser("unstamped_vs_stamped", premium=True)
push([prof("A", "Stamped", at=T2)], at=T2)
client.put("/profile/growth", json={"growth_state": {
    "person": {"name": "P"}, "profiles": [{"id": "A", "name": "Unstamped"}],
    "activeProfileId": "A"}})
check("unstamped push does NOT overwrite a timestamped stored body",
      find_stored(u, "A").get("profileName") == "Stamped",
      str(find_stored(u, "A").get("profileName")))

# ══════════════════════════════════════════════════════════════════════════
section("C. Creating additional profiles is Premium, server-enforced")
u = mkuser("free_create", premium=False)
# The BOOTSTRAP exception: local-first onboarding creates the first profile
# on the device before any account exists, so a free user must be able to
# sync that one. Rejecting it would strand every new free user — the client
# cannot delete it either (deleteProfile refuses the last profile).
push([prof("A", "Money")], at=T1)
check("free user's first/bootstrap profile is accepted", stored_ids(u) == ["A"], str(stored_ids(u)))

r = client.post("/profile/profiles", json={"profile": prof("NEW", "Extra")})
check("canonical create 403s for a free user", r.status_code == 403, str(r.status_code))
check("403 carries the structured premium code",
      (r.json().get("detail") or {}).get("code") == "premium_required", str(r.json()))
check("no profile was created", stored_ids(u) == ["A"], str(stored_ids(u)))

# Defence-in-depth: the generic PUT filters unentitled new ids, but must
# still apply the valid edits bundled into the same push.
r = push([prof("A", "Money", at=T2, ledger=["valid-edit"]), prof("NEW", "Extra", at=T2)], at=T2)
check("generic push returns 200 (never a 4xx)", r.status_code == 200, str(r.status_code))
check("unentitled new id filtered out", stored_ids(u) == ["A"], str(stored_ids(u)))
check("rejected id reported for client reconciliation",
      r.json().get("rejectedProfileIds") == ["NEW"], str(r.json().get("rejectedProfileIds")))
check("valid edit in the SAME push still applied",
      find_stored(u, "A").get("ledger") == ["valid-edit"])
check("entitlement echoed to the client", r.json().get("effectivePremium") is False)

# A free user may still delete.
push([prof("A", "Money", at=T2)], at=T2, deleted=["GONE"])
check("free user's tombstone honoured",
      "GONE" in (db.query(Profile).filter(Profile.user_id == u).first().deleted_profile_ids or []))

set_premium(u, True)
r = client.post("/profile/profiles", json={"profile": prof("NEW", "Extra")})
check("canonical create succeeds once entitled", r.status_code == 200, str(r.status_code))
check("new profile stored", stored_ids(u) == ["A", "NEW"], str(stored_ids(u)))

# ══════════════════════════════════════════════════════════════════════════
section("D. Assignment is Premium; uploads never fail")
u = mkuser("assign_user", premium=True)
# Two real profiles, created while entitled (the bootstrap exception admits
# only ONE, so a free user genuinely cannot end up with two — which is the
# point of section C).
push([prof("A", "Money"), prof("B", "Career")], active="A", at=T1)
check("premium user has both profiles", stored_ids(u) == ["A", "B"], str(stored_ids(u)))
set_premium(u, False)

r = client.post("/library/", json={"title": "Free book", "type": "text",
                                   "content": "x", "mode": "wisdom",
                                   "growth_profile_id": "B"})
check("free create succeeds (never 403)", r.status_code == 200, str(r.status_code))
check("client's unentitled choice IGNORED, default substituted",
      r.json().get("growth_profile_id") == "A", str(r.json().get("growth_profile_id")))
check("derived name matches the substituted profile",
      r.json().get("growth_profile_name") == "Money")
free_item_id = r.json()["id"]

# Rollout flag defaults to OFF (plan Phase 10): a shipped pre-feature client
# tapping this used to succeed unconditionally, so it must keep succeeding
# post-deploy — never a NEW 403 for a tap that used to work — until the flag
# is explicitly flipped once the four Phase-10 step-3 conditions are live.
from app.config import get_settings  # noqa: E402
_settings = get_settings()
check("strict_assignment_enforcement defaults to False",
      _settings.strict_assignment_enforcement is False)

r = client.patch("/library/%s" % free_item_id, json={"growth_profile_id": "B"})
check("free PATCH of the assignment TOLERATES (200) while enforcement is off",
      r.status_code == 200, str(r.status_code))
check("client's unentitled choice IGNORED even in tolerant mode — never honors B",
      r.json().get("growth_profile_id") == "A", str(r.json().get("growth_profile_id")))
check("assignment unchanged (still the default, not B)", item(free_item_id).growth_profile_id == "A")

_settings.strict_assignment_enforcement = True
try:
    r = client.patch("/library/%s" % free_item_id, json={"growth_profile_id": "B"})
    check("free PATCH of the assignment 403s once enforcement is on", r.status_code == 403, str(r.status_code))
    check("403 carries premium_required",
          (r.json().get("detail") or {}).get("code") == "premium_required", str(r.json()))
    check("assignment unchanged", item(free_item_id).growth_profile_id == "A")
finally:
    _settings.strict_assignment_enforcement = False

r = client.patch("/library/%s" % free_item_id, json={"title": "Renamed"})
check("free PATCH of title still works", r.status_code == 200, str(r.status_code))

set_premium(u, True)
r = client.patch("/library/%s" % free_item_id, json={"growth_profile_id": "B"})
check("premium PATCH of the assignment succeeds", r.status_code == 200, str(r.status_code))
check("assignment applied", item(free_item_id).growth_profile_id == "B")
check("name re-derived server-side", item(free_item_id).growth_profile_name == "Career")

r = client.patch("/library/%s" % free_item_id, json={"growth_profile_id": None})
check("explicit null RESETS to the default profile",
      item(free_item_id).growth_profile_id == "A", str(item(free_item_id).growth_profile_id))

r = client.patch("/library/%s" % free_item_id, json={"title": "Only a title"})
check("omitted assignment field leaves it unchanged",
      item(free_item_id).growth_profile_id == "A")

r = client.patch("/library/%s" % free_item_id, json={"growth_profile_id": "GHOST"})
check("unknown profile id refused", r.status_code == 400, str(r.status_code))

r = client.post("/library/", json={"title": "Story", "type": "text", "content": "x",
                                   "mode": "story", "growth_profile_id": "A"})
check("story book keeps BOTH assignment fields null",
      r.json().get("growth_profile_id") is None and r.json().get("growth_profile_name") is None)

# ══════════════════════════════════════════════════════════════════════════
section("E. Rename propagates; delete reassigns; duplicates refused")
u = mkuser("integrity_user", premium=True)
push([prof("A", "Money"), prof("B", "Career")], active="A", at=T1)
mkitem(u, "bk1", gp_id="A", gp_name="Money")
mkitem(u, "bk2", gp_id="A", gp_name="Money")
mkitem(u, "bk3", gp_id="B", gp_name="Career")

client.patch("/profile/profiles/A", json={"name": "Wealth"})
check("rename refreshed EVERY assigned book, in one transaction",
      item("bk1").growth_profile_name == "Wealth" and item("bk2").growth_profile_name == "Wealth",
      "%s / %s" % (item("bk1").growth_profile_name, item("bk2").growth_profile_name))
check("books keep their stable id across a rename (never orphaned)",
      item("bk1").growth_profile_id == "A")
check("an unrelated profile's books are untouched",
      item("bk3").growth_profile_name == "Career")

r = client.patch("/profile/profiles/B", json={"name": "wealth"})
check("duplicate name refused (case-insensitive)", r.status_code == 409, str(r.status_code))
check("409 carries a structured code",
      (r.json().get("detail") or {}).get("code") == "duplicate_profile_name")

r = client.post("/profile/profiles", json={"profile": prof("C", "  WEALTH  ")})
check("duplicate name refused on create too", r.status_code == 409, str(r.status_code))

r = client.delete("/profile/profiles/A")
check("delete succeeds", r.status_code == 200, str(r.status_code))
check("its books were reassigned deterministically",
      item("bk1").growth_profile_id == "B" and item("bk2").growth_profile_id == "B",
      "%s / %s" % (item("bk1").growth_profile_id, item("bk2").growth_profile_id))
check("reassigned books carry the new derived name",
      item("bk1").growth_profile_name == "Career")
check("deleted id is tombstoned",
      "A" in (db.query(Profile).filter(Profile.user_id == u).first().deleted_profile_ids or []))

r = client.delete("/profile/profiles/B")
check("refuses to delete the LAST profile", r.status_code == 409, str(r.status_code))

# ══════════════════════════════════════════════════════════════════════════
section("F. Bootstrap repair vs unresolved-legacy (the discriminator)")
u = mkuser("repair_user", premium=True)
# Rows that existed BEFORE any profile reached the server.
mkitem(u, "boot1")                                    # both fields NULL
mkitem(u, "boot2", mode="story")                      # story: must stay null
mkitem(u, "legacy_unmatched", gp_name="Nonexistent")  # name, no match
mkitem(u, "legacy_matched", gp_name="Money")          # name, will match

push([prof("A", "Money"), prof("B", "Career")], active="A", at=T1)

check("bootstrap row attached to the default profile",
      item("boot1").growth_profile_id == "A", str(item("boot1").growth_profile_id))
check("story book NOT attached", item("boot2").growth_profile_id is None)
check("legacy row with a UNIQUE name is promoted",
      item("legacy_matched").growth_profile_id == "A",
      str(item("legacy_matched").growth_profile_id))
check("unmatched legacy row is LEFT ALONE (id still null)",
      item("legacy_unmatched").growth_profile_id is None)
check("...and its original name is PRESERVED, not overwritten",
      item("legacy_unmatched").growth_profile_name == "Nonexistent",
      str(item("legacy_unmatched").growth_profile_name))

# Ambiguity: two live profiles share a name, so a row naming it can't be
# promoted without guessing.
u2 = mkuser("ambig_user", premium=True)
mkitem(u2, "ambig", gp_name="Money")
push([prof("A", "Money"), prof("B", "Money", at=T1)], active="A", at=T1)
check("ambiguous legacy name is NOT promoted (never guesses)",
      item("ambig").growth_profile_id is None, str(item("ambig").growth_profile_id))
check("ambiguous row keeps its name", item("ambig").growth_profile_name == "Money")

# Once the duplicate is gone the name is unique again, so it can be promoted.
push([prof("A", "Money", at=T2)], active="A", at=T2, deleted=["B"])
check("becomes promotable once the ambiguity is resolved",
      item("ambig").growth_profile_id == "A", str(item("ambig").growth_profile_id))

section("F2. /profile/growth/ensure is idempotent")
u = mkuser("ensure_user", premium=False)
mkitem(u, "pre_boot")  # uploaded before any profile reached the server
r = client.post("/profile/growth/ensure",
                json={"growth_state": {"person": {"name": "P"},
                                       "profiles": [prof("A", "Money")],
                                       "activeProfileId": "A", "updatedAt": T1}})
check("ensure creates the row", r.status_code == 200, str(r.status_code))
check("ensure attaches the pre-existing bootstrap book",
      item("pre_boot").growth_profile_id == "A", str(item("pre_boot").growth_profile_id))
before = stored_ids(u)
r = client.post("/profile/growth/ensure",
                json={"growth_state": {"person": {"name": "P"},
                                       "profiles": [prof("Z", "Other")],
                                       "activeProfileId": "Z", "updatedAt": T2}})
check("ensure is idempotent — does not add a second profile",
      stored_ids(u) == before, "%s vs %s" % (stored_ids(u), before))


# ══════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
if failures:
    print("FAILED (%d): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
