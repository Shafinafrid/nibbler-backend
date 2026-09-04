"""
Growth profiles plan Phase 8 — test_profile_attribution_chain.py.

The two bullets from the plan's regression list (§"Already fixed on
origin/main") that were genuinely never covered by an end-to-end test:

  · a scheduler-generated session's `profile` dict carries `id` — proven
    here via the SAME `_build_profile_dict`/`_pick_profile` the scheduler
    itself calls (app/services/notification_service.py), confirming they
    delegate to the shared `resolve_assigned_profile` resolver (Sep 2026)
    rather than the old private name-only match that had no `id` at all.
  · `PersonalizeAnswerResponse.profile_id` is present on EVERY response
    shape the /personalize-answer endpoint can return — first answer,
    replayed answer (already 'answered'), concurrent-loser replay, AND the
    dedicated 409 for an unattributable row (a pre-fix version returned a
    bare 200 with profile_id=None here instead) — never a bare 200 with a
    null profile_id.

Card-level `profileId` (the OTHER half of this file's plan name) is already
covered by tests/test_personalization_reaudit_fixes.py; not duplicated here.
The five-step resolver precedence and session-generation/Connect parity are
covered by tests/test_growth_profile_session_authority.py; not duplicated
here either. This file exists ONLY for the two gaps above.

    .venv/bin/python tests/test_profile_attribution_chain.py
"""
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/gp_attribution_chain.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app.database import create_tables, SessionLocal, get_db  # noqa: E402
from app.middleware.auth import get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
from app.models.personalization import PersonalizationQuestion  # noqa: E402
from app.services import notification_service as ns  # noqa: E402
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


def mkitem(uid, item_id, *, gp_id=None, gp_name=None):
    db.add(LibraryItem(
        id=item_id, user_id=uid, title=item_id, type="text",
        content="Body text. " * 40, mode="wisdom", processed=True, is_active=True,
        growth_profile_id=gp_id, growth_profile_name=gp_name,
        is_unlocked_selection=True,
    ))
    db.commit()


def mkbite(uid, item_id, bite_id):
    # daily_bites has a real UNIQUE (user_id, library_item_id, date)
    # constraint (Task 10 hardening) — each bite in this file needs its OWN
    # library item, since several are created for the same user on the same
    # test-run day.
    db.add(DailyBite(
        id=bite_id, user_id=uid, library_item_id=item_id,
        date=datetime.utcnow().date(),
        title="T", insight="I", reflection="R", action="A",
        source=item_id, theme="wisdom",
    ))
    db.commit()


def mkquestion(uid, bite_id, *, profile_id, options=None):
    q = PersonalizationQuestion(
        id="pq-" + uuid.uuid4().hex[:8], user_id=uid, daily_bite_id=bite_id,
        profile_id=profile_id, question="What resonates?",
        options=options or [
            {"id": "o1", "text": "This", "tag": "increase_confidence"},
            {"id": "o2", "text": "That", "tag": "increase_challenge"},
        ],
        status="pending",
    )
    db.add(q)
    db.commit()
    return q.id


# ══════════════════════════════════════════════════════════════════════════
section("A. The scheduler dict carries `id` — via the SAME resolver/builder the real scheduler calls")
# ══════════════════════════════════════════════════════════════════════════
u = mkuser("attr_sched", premium=True)
push_profiles(u, [prof("A", "Money"), prof("B", "Career")], active="A")
mkitem(u, "sched_book", gp_id="B")

# The exact private helpers app/services/notification_service.py's real
# scheduler pass calls (_notify_delivery_slot -> generate_session_for_item,
# and the read-path building the personalize card's `profile` field) —
# calling them directly here (not the 5-minute cron) isolates the dict
# SHAPE this bullet is about from the scheduler's own timing/tick logic,
# which is exercised elsewhere.
growth_state = db.query(__import__("app.models.profile", fromlist=["Profile"]).Profile) \
    .filter_by(user_id=u).first().growth_state
item_row = db.query(LibraryItem).filter_by(id="sched_book").first()

profile_dict = ns._build_profile_dict(growth_state, item_row)
check("the scheduler's profile dict carries a non-null id", profile_dict.get("id") == "B", profile_dict)
check("...and it's the book's OWN assignment (B), not the active profile (A) — "
      "proving this delegates to the shared resolver, not the old active-only shortcut",
      profile_dict.get("id") != "A", profile_dict)

read_length = ns._read_length_for(growth_state, item_row)
check("_read_length_for (same _pick_profile call) does not throw and returns a real value",
      read_length in (5, 10, 15), read_length)


# ══════════════════════════════════════════════════════════════════════════
section("B. PersonalizeAnswerResponse.profile_id is present on EVERY response shape — never a bare 200 with null")
# ══════════════════════════════════════════════════════════════════════════
u = mkuser("attr_answer", premium=True)
push_profiles(u, [prof("A", "Money")], active="A")

# B1 — first (fresh) answer via a listed option.
mkitem(u, "ans_book_1", gp_id="A")
bite1 = "bite-" + uuid.uuid4().hex[:8]
mkbite(u, "ans_book_1", bite1)
qid1 = mkquestion(u, bite1, profile_id="A")
r = client.post("/bites/%s/personalize-answer" % bite1, json={"option_id": "o1"})
check("first answer succeeds", r.status_code == 200, r.text)
check("profile_id is present and correct on a FRESH answer", r.json().get("profile_id") == "A", r.json())

# B2 — replay of an already-answered row must return the SAME profile_id,
# never null, never a re-interpretation.
r2 = client.post("/bites/%s/personalize-answer" % bite1, json={"option_id": "o1"})
check("replaying an already-answered question succeeds (idempotent)", r2.status_code == 200, r2.text)
check("profile_id is STILL present on the replayed (already-answered) response",
      r2.json().get("profile_id") == "A", r2.json())

# B3 — a row with NO profile_id at all (a pre-attribution-fix legacy row, or
# a scheduler session generated before growth_state ever synced) must be a
# 409 'personalize_unavailable', NEVER a 200 with profile_id=None — the
# exact bug this whole file exists to prove is closed.
mkitem(u, "ans_book_2", gp_id="A")
bite2 = "bite-" + uuid.uuid4().hex[:8]
mkbite(u, "ans_book_2", bite2)
qid2 = mkquestion(u, bite2, profile_id=None)
r3 = client.post("/bites/%s/personalize-answer" % bite2, json={"option_id": "o1"})
check("an unattributable (profile_id=None) question is REJECTED, not silently answered",
      r3.status_code == 409, r3.text)
check("the rejection carries the structured code",
      (r3.json().get("detail") or {}).get("code") == "personalize_unavailable", r3.json())
check("the row was NOT mutated by the rejected attempt (still pending, not answered)",
      db.query(PersonalizationQuestion).filter_by(id=qid2).first().status == "pending")

# B4 — a concurrent-loser replay (someone else's claim already answered the
# row while this request was deciding) must ALSO carry profile_id — exercised
# via the exact code path: pre-mark the row 'answered' with a real
# profile_id, matching what _finalize_personalization_answer's replay branch
# reads back.
mkitem(u, "ans_book_3", gp_id="A")
bite3 = "bite-" + uuid.uuid4().hex[:8]
mkbite(u, "ans_book_3", bite3)
qid3 = mkquestion(u, bite3, profile_id="A")
row3 = db.query(PersonalizationQuestion).filter_by(id=qid3).first()
row3.status = "answered"
row3.applied_tags = ["increase_confidence"]
row3.interpreted_summary = None
db.commit()
r4 = client.post("/bites/%s/personalize-answer" % bite3, json={"option_id": "o1"})
check("a request against an ALREADY-answered row (simulating a concurrent-loser replay) succeeds",
      r4.status_code == 200, r4.text)
check("profile_id is present on this replay path too", r4.json().get("profile_id") == "A", r4.json())

# B5 — free-text path (goes through the LLM interpretation branch, a
# DIFFERENT code path from the option_id branch above) must also stamp
# profile_id. Stub the LLM call so this test has no network/API dependency.
from app.services.llm import LLMService  # noqa: E402
LLMService.interpret_personalization_answer = \
    lambda self, **kwargs: {"tags": ["increase_confidence"], "summary": "stubbed"}

mkitem(u, "ans_book_4", gp_id="A")
bite4 = "bite-" + uuid.uuid4().hex[:8]
mkbite(u, "ans_book_4", bite4)
qid4 = mkquestion(u, bite4, profile_id="A")
r5 = client.post("/bites/%s/personalize-answer" % bite4, json={"free_text": "I want to understand this better"})
check("the free-text (LLM-interpretation) path succeeds", r5.status_code == 200, r5.text)
check("profile_id is present on the free-text path too — a DIFFERENT code branch from option_id",
      r5.json().get("profile_id") == "A", r5.json())


print("\n" + "=" * 70)
if failures:
    print("FAILED (%d): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL CHECKS PASSED")
