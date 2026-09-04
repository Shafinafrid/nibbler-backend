"""
Server-authoritative profile resolution for SESSION GENERATION (Sep 2026).

Closes a gap the growth-profiles feature's Phase 3/8 named it but never
actually wired up: `/connect/insights` and `/connect/stats` were made
server-authoritative (they resolve the book's assigned profile from OUR OWN
data and ignore anything the client sends), but `POST /bites/session` —
session GENERATION itself, and the personalization question it may attach —
still built its `profile` dict straight from the client-supplied
`growth_profile` request field. A stale or spoofed client value could steer
which goal a nibble (and its personalization question) was generated for,
even when the book's real server-side assignment said otherwise. Fixed in
app/routers/bites.py's `get_or_create_session` by resolving via the SAME
`resolve_assigned_profile`/`build_profile_payload` helpers Connect and the
scheduler already used, falling back to the client's value ONLY in the
genuine pre-first-sync bootstrap window (zero server profiles yet, §1.3).

Covers, in order:
  A. The core fix — a book assigned to profile A generates its session (and
     any personalization question) attributed to A even when the request
     claims a different, unrelated profile B.
  B. Bootstrap fallback — a brand-new user with ZERO server profiles still
     gets a session generated from their client-sent profile; never a
     failure because of the sync-timing gap.
  C. `resolve_assigned_profile`'s five-step precedence, exercised directly
     and in isolation: stable id -> unique legacy name -> active -> first ->
     none.
  D. Goal-passage provenance end-to-end via real HTTP: a passage generated
     under profile A is not surfaced by /connect/stats after the book is
     reassigned to B; a legacy NULL-stamped passage stays hidden; the
     passage reappears once a nibble exists under the new profile.
  E. /connect/insights ignores a client-supplied growth_profile object,
     confirmed via response identity (resolved_profile_id echoes the
     SERVER'S resolution, never anything the request body claimed).

    .venv/bin/python tests/test_growth_profile_session_authority.py
"""

import os
import sys
import tempfile
from datetime import date, datetime, timedelta

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/gp_session_auth.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app.database import create_tables, SessionLocal, get_db  # noqa: E402
from app.middleware.auth import get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.profile import Profile  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
from app.services import session_service as ss  # noqa: E402
from app.services.embedding_service import EmbeddingService  # noqa: E402
from app.services.profile_resolution import resolve_assigned_profile  # noqa: E402
import main  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  — " + str(detail)) if detail else ""))
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

T1 = "2026-09-02T10:00:00.000Z"


def mkuser(uid, premium=True):
    u = User(id=uid, email="%s@example.com" % uid)
    u.premium_until = datetime.utcnow() + (timedelta(days=30) if premium else timedelta(days=-30))
    db.add(u)
    db.commit()
    ACTIVE["id"] = uid
    return uid


def prof(pid, name, **extra):
    d = {"id": pid, "name": name, "profileName": name, "updatedAt": T1}
    d.update(extra)
    return d


def push_profiles(uid, profiles, active=None):
    ACTIVE["id"] = uid
    gs = {"person": {"name": "P"}, "profiles": profiles,
          "activeProfileId": active or profiles[0]["id"], "updatedAt": T1}
    r = client.put("/profile/growth", json={"growth_state": gs})
    assert r.status_code == 200, r.text
    return r


def mkitem(uid, item_id, *, gp_id=None, gp_name=None, mode="wisdom", content=None, unlocked=True):
    db.add(LibraryItem(
        id=item_id, user_id=uid, title=item_id, type="text",
        content=content or ("A distinctive passage about the topic. " * 60),
        mode=mode, processed=True, is_active=True,
        growth_profile_id=gp_id, growth_profile_name=gp_name,
        is_unlocked_selection=unlocked,
    ))
    db.commit()


def item(item_id):
    db.expire_all()
    return db.query(LibraryItem).filter(LibraryItem.id == item_id).first()


def bites_for(uid, item_id):
    db.expire_all()
    return (
        db.query(DailyBite)
        .filter(DailyBite.user_id == uid, DailyBite.library_item_id == item_id)
        .order_by(DailyBite.generated_at.desc())
        .all()
    )


# ── Fast, deterministic stand-in for the real LLM deck call ────────────────
_captured_profile = {"last": None}


def _fake_generate_wisdom_session(self, **kwargs):
    _captured_profile["last"] = kwargs.get("profile")
    return {
        "title": "Session Title", "chapter": "Ch 1", "headline": "Headline.",
        "preview": "Preview.",
        "cards": [
            {"kind": "hook", "eyebrow": "TODAY'S SESSION", "title": "Hook",
             "body": "Body.", "highlight": None, "options": None, "explanation": None},
            {"kind": "summary", "eyebrow": "SESSION SUMMARY", "title": "Summary",
             "body": "Body.", "highlight": None, "options": None, "explanation": None},
        ],
        "quiz": None,
    }


ss.LLMService.generate_wisdom_session = _fake_generate_wisdom_session
# No Pinecone/Voyage key in the test env, so search_item_fresh already
# returns [] and generation falls back to raw item.content — real behavior
# for the keyless-dev case, exercised as-is (no mock needed for A/B/D/E).


def next_day(n):
    return (date.today() + timedelta(days=n)).isoformat()


# ══════════════════════════════════════════════════════════════════════════
section("A. Session generation resolves the profile SERVER-SIDE, ignoring a spoofed client value")
u = mkuser("session_auth_a", premium=True)
push_profiles(u, [prof("A", "Money"), prof("EVIL", "Attacker's Choice")], active="A")
mkitem(u, "book_a", gp_id="A", gp_name="Money")

# The client claims profile EVIL — a value that does not match the book's
# real server-side assignment (A). Before the fix this drove generation.
r = client.post("/bites/session", json={
    "library_item_id": "book_a", "read_length": 5,
    "client_date": next_day(0),
    "growth_profile": {"id": "EVIL", "name": "Attacker's Choice",
                        "lifeArea": "Nothing Real", "aspirationLabel": "Fake goal"},
})
check("session generation succeeds", r.status_code == 200, r.text)
check("the LLM call was grounded in the SERVER-RESOLVED profile (A), not the spoofed one (EVIL)",
      (_captured_profile["last"] or {}).get("id") == "A",
      _captured_profile["last"])
check("...never the client's claimed id",
      (_captured_profile["last"] or {}).get("id") != "EVIL")
bite_row = bites_for(u, "book_a")[0]
check("the persisted DailyBite is stamped with the server-resolved profile id",
      bite_row.growth_profile_id == "A", bite_row.growth_profile_id)


# ══════════════════════════════════════════════════════════════════════════
section("B. Bootstrap fallback — zero server profiles yet, session still generates")
u = mkuser("session_auth_boot", premium=False)
# Deliberately NO push_profiles call — this account has no Profile row at
# all yet, the genuine pre-first-sync window (plan §1.3).
mkitem(u, "book_boot")
r = client.post("/bites/session", json={
    "library_item_id": "book_boot", "read_length": 5,
    "client_date": next_day(0),
    "growth_profile": {"id": "LOCAL-ONLY", "name": "Onboarding Profile",
                        "lifeArea": "Focus", "aspirationLabel": "Build a habit"},
})
check("session generation never fails because of the sync-timing gap",
      r.status_code == 200, r.text)
check("falls back to the client's pre-sync profile when the server has none",
      (_captured_profile["last"] or {}).get("id") == "LOCAL-ONLY",
      _captured_profile["last"])


# ══════════════════════════════════════════════════════════════════════════
section("C. resolve_assigned_profile — the five-step precedence, in isolation")


class _Item:
    def __init__(self, gp_id=None, gp_name=None):
        self.growth_profile_id = gp_id
        self.growth_profile_name = gp_name


gs_c = {
    "profiles": [prof("A", "Money"), prof("B", "Career")],
    "activeProfileId": "B",
}

check("step 1 — stable id wins even when a different name is also present",
      resolve_assigned_profile(gs_c, _Item(gp_id="A", gp_name="Career")).get("id") == "A")

check("step 2 — unique legacy name matches when id is absent",
      resolve_assigned_profile(gs_c, _Item(gp_id=None, gp_name="Money")).get("id") == "A")

gs_c_ambig = {
    "profiles": [prof("A", "Money"), prof("C", "Money")],
    "activeProfileId": "A",
}
check("step 2 — ambiguous legacy name is SKIPPED, never guessed",
      resolve_assigned_profile(gs_c_ambig, _Item(gp_id=None, gp_name="Money")) is None
      or resolve_assigned_profile(gs_c_ambig, _Item(gp_id=None, gp_name="Money")).get("id") == "A",
      "falls through to step 3 (active), which happens to also be A here — see next check for a clean separation")

gs_c_ambig2 = {
    "profiles": [prof("A", "Money"), prof("C", "Money")],
    "activeProfileId": "C",
}
check("step 2 — ambiguous name skipped, falls through to step 3 (active), NOT the first ambiguous match",
      resolve_assigned_profile(gs_c_ambig2, _Item(gp_id=None, gp_name="Money")).get("id") == "C")

check("step 3 — no id, no name -> falls to activeProfileId",
      resolve_assigned_profile(gs_c, _Item()).get("id") == "B")

gs_c_no_active = {
    "profiles": [prof("A", "Money"), prof("B", "Career")],
    "activeProfileId": "NONEXISTENT",
}
check("step 4 — activeProfileId doesn't resolve -> falls to the first profile",
      resolve_assigned_profile(gs_c_no_active, _Item()).get("id") == "A")

check("step 5 — no live profiles at all -> None",
      resolve_assigned_profile({"profiles": []}, _Item()) is None)

check("a tombstoned id is treated as not-live for step 1",
      resolve_assigned_profile(gs_c, _Item(gp_id="A"), tombstones={"A"}).get("id") == "B",
      "id A is tombstoned, so resolution should NOT return it — falls through to active (B)")


# ══════════════════════════════════════════════════════════════════════════
section("D. Goal-passage provenance end-to-end (via real HTTP)")
u = mkuser("provenance_user", premium=True)
push_profiles(u, [prof("A", "Money"), prof("B", "Career")], active="A")
mkitem(u, "prov_book", gp_id="A", gp_name="Money")

# Session 1, under profile A — stamps a goal_passage on the bite.
r1 = client.post("/bites/session", json={"library_item_id": "prov_book", "read_length": 5, "client_date": next_day(0)})
check("first session (profile A) generated", r1.status_code == 200, r1.text)
b1 = bites_for(u, "prov_book")[0]
# /connect/stats only considers READ sessions (read_at set) — mark it read,
# matching what the app does when the user actually opens the nibble.
r = client.post("/bites/%s/read" % b1.id)
check("bite marked read", r.status_code == 200, r.text)
# goal_passage isn't populated by the fake LLM stub (it's derived from real
# retrieval), so stamp it directly to exercise the STATS filtering logic —
# the provenance stamping itself is already proven by section A above.
b1.goal_passage = "An excerpt that spoke to the MONEY goal."
db.commit()

r = client.get("/connect/stats/prov_book")
check("stats endpoint reachable", r.status_code == 200, r.text)
check("goal passage IS shown while the book is still assigned to A",
      (r.json().get("goal_passage") or {}).get("text") == "An excerpt that spoke to the MONEY goal.",
      r.json().get("goal_passage"))

# Reassign the book to profile B — the profile-A passage must now be hidden.
r = client.patch("/library/prov_book", json={"growth_profile_id": "B"})
check("book reassigned to profile B", r.status_code == 200, r.text)

r = client.get("/connect/stats/prov_book")
check("the profile-A goal passage is NO LONGER shown after reassignment to B",
      r.json().get("goal_passage") is None, r.json().get("goal_passage"))
check("resolved_profile_id now reflects B",
      r.json().get("resolved_profile_id") == "B", r.json().get("resolved_profile_id"))

# A legacy row (growth_profile_id NULL) must stay hidden even though it has
# text — no way to know which profile it was really written for.
b1.growth_profile_id = None
db.commit()
r = client.get("/connect/stats/prov_book")
check("a legacy NULL-stamped passage stays hidden, never guessed at",
      r.json().get("goal_passage") is None, r.json().get("goal_passage"))

# A fresh session generated NOW (book is assigned to B) should surface once
# it carries a real goal_passage under B.
r2 = client.post("/bites/session", json={"library_item_id": "prov_book", "read_length": 5, "client_date": next_day(1)})
check("second session (profile B) generated", r2.status_code == 200, r2.text)
b2 = [b for b in bites_for(u, "prov_book") if b.id != b1.id][0]
check("the new session was stamped with the NEW resolved profile (B)",
      b2.growth_profile_id == "B", b2.growth_profile_id)
r = client.post("/bites/%s/read" % b2.id)
check("second bite marked read", r.status_code == 200, r.text)
b2.goal_passage = "An excerpt that spoke to the CAREER goal."
db.commit()
r = client.get("/connect/stats/prov_book")
check("the matching-profile passage reappears once a real nibble exists under the current assignment",
      (r.json().get("goal_passage") or {}).get("text") == "An excerpt that spoke to the CAREER goal.",
      r.json().get("goal_passage"))


# ══════════════════════════════════════════════════════════════════════════
section("E. /connect/insights ignores a client-supplied growth_profile object")
u = mkuser("insights_auth_user", premium=True)
push_profiles(u, [prof("A", "Money"), prof("EVIL2", "Attacker's Choice")], active="A")
mkitem(u, "insights_book", gp_id="A", gp_name="Money")

r = client.post("/connect/insights", json={
    "library_item_id": "insights_book",
    "growth_profile": {"id": "EVIL2", "name": "Attacker's Choice"},
})
check("insights endpoint reachable", r.status_code == 200, r.text)
check("resolved_profile_id reflects the SERVER'S real assignment (A), never the request body's claim",
      r.json().get("resolved_profile_id") == "A", r.json())


# ══════════════════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
if failures:
    print("FAILED (%d): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
