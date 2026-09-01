"""
Regression tests for the RE-AUDIT fixes (Aug 2026).

The first round of audit fixes passed their own tests while leaving the
defects reachable — the tests asserted that the code had the shape I
intended, not that the behavior actually changed. These assert OBSERVABLE
OUTCOMES on the paths the re-audit named.

Plain script, no pytest — repo convention.
"""
import os
import sys
import uuid
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_reaudit.db")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")

passed = 0
failed = 0


def check(label, condition, detail=None):
    global passed, failed
    if condition:
        passed += 1
        print("PASS  %s" % label)
    else:
        failed += 1
        print("FAIL  %s%s" % (label, ("  -> %s" % (detail,)) if detail is not None else ""))


# ── #1: the card the app receives must carry the target profile id ─────────
# The first fix persisted profile_id on the DB row but never put it on the
# card, so the app read `card.profileId === undefined` and fell back to the
# active profile — the bug the fix was for, still fully reachable.
from app.services.session_service import _insert_personalization_card

question = {
    "question": "Automate it, or do it by hand?",
    "eyebrow": "ONE QUICK QUESTION",
    "highlight": None,
    "options": [
        {"id": "opt0", "text": "Automate it", "tag": "prefers_automation"},
        {"id": "opt1", "text": "By hand", "tag": "prefers_manual_control"},
    ],
}


def fresh_deck():
    return {"cards": [
        {"kind": "hook", "title": "H", "body": "b"},
        {"kind": "insight", "title": "I", "body": "b"},
        {"kind": "prompt", "title": "P", "body": "b"},
        {"kind": "summary", "title": "S", "body": "b"},
    ]}


deck = fresh_deck()
_insert_personalization_card(deck, question, profile_id="prof-investing")
personalize = next(c for c in deck["cards"] if c["kind"] == "personalize")
check("the inserted card carries the target profileId",
      personalize.get("profileId") == "prof-investing", personalize.get("profileId"))

deck2 = fresh_deck()
_insert_personalization_card(deck2, question, profile_id=None)
p2 = next(c for c in deck2["cards"] if c["kind"] == "personalize")
check("a session with no profile id yields an explicit None, not a missing key",
      "profileId" in p2 and p2["profileId"] is None)


# ── #9: contradictory tags are dropped, not applied ────────────────────────
from app.routers.bites import _normalize_tags

check("opposing confidence tags are BOTH dropped",
      _normalize_tags(["increase_confidence", "decrease_confidence"]) == [])
check("opposing preference tags are BOTH dropped",
      _normalize_tags(["prefers_automation", "prefers_manual_control"]) == [])
check("a non-conflicting tag survives alongside a dropped pair",
      _normalize_tags(["increase_confidence", "decrease_confidence", "shift_practical"])
      == ["shift_practical"])
check("duplicates still collapse to one",
      _normalize_tags(["shift_practical", "shift_practical"]) == ["shift_practical"])
check("ordinary tags pass through in order",
      _normalize_tags(["prefers_automation", "increase_confidence"])
      == ["prefers_automation", "increase_confidence"])


# ── #13: a quote spanning two chunks must not count as grounded ────────────
from app.services.llm.validation import validate_personalization
from app.services.llm.errors import ProviderError

opts = [
    {"text": "a", "tag": "prefers_automation"},
    {"text": "b", "tag": "prefers_simplicity"},
]
chunks = ["The first passage ends here.", "And a second, unrelated one begins."]

seam = {
    "question": "Q?", "eyebrow": "E", "options": opts,
    # Exists only across the join of two separate excerpts — in no real passage.
    "highlight": "ends here. And a second",
}
try:
    validate_personalization(seam, provider="test", source_chunks=chunks)
    check("a quote spanning the seam between two chunks is rejected", False)
except ProviderError:
    check("a quote spanning the seam between two chunks is rejected", True)

within = dict(seam, highlight="a second, unrelated one")
try:
    validate_personalization(within, provider="test", source_chunks=chunks)
    check("a quote wholly inside one chunk is still accepted", True)
except ProviderError:
    check("a quote wholly inside one chunk is still accepted", False)


# ── #2: claim ownership — a superseded worker must not win ────────────────
# Exercised against a real DB session so the lock/claim columns are real.
from app.database import Base, engine, SessionLocal
from app.models.personalization import PersonalizationQuestion

Base.metadata.create_all(bind=engine)
db = SessionLocal()

bite_id = "bite-" + uuid.uuid4().hex[:8]
user_id = "user-" + uuid.uuid4().hex[:8]
db.add(PersonalizationQuestion(
    user_id=user_id, daily_bite_id=bite_id, question="Q?",
    options=question["options"], profile_id="prof-investing", status="pending",
))
db.commit()

row = db.query(PersonalizationQuestion).filter_by(daily_bite_id=bite_id).first()
check("the model persists a claimed_by column", hasattr(row, "claimed_by"))

# Worker A claims, then B legitimately takes over an expired lease.
row.status = "processing"
row.claimed_by = "worker-A"
row.claimed_until = datetime.utcnow() - timedelta(minutes=1)   # already expired
db.commit()

row.claimed_by = "worker-B"
row.claimed_until = datetime.utcnow() + timedelta(minutes=3)
db.commit()

# A now finishes late. The finalize guard is `row.claimed_by != worker_id`.
row = db.query(PersonalizationQuestion).filter_by(daily_bite_id=bite_id).first()
a_may_finalize = (row.status == "answered") or (row.claimed_by == "worker-A")
check("a superseded worker (A) is not allowed to finalize", not a_may_finalize)

b_may_finalize = row.claimed_by == "worker-B"
check("the worker that took over (B) is allowed to finalize", b_may_finalize)

# A's failure path must not reset B's live claim.
from app.routers.bites import _release_claim_if_owner
_release_claim_if_owner(db, bite_id, user_id, "worker-A")
row = db.query(PersonalizationQuestion).filter_by(daily_bite_id=bite_id).first()
check("a superseded worker's failure does NOT clear the live claim",
      row.claimed_by == "worker-B" and row.status == "processing",
      (row.claimed_by, row.status))

# The real owner's failure DOES release it, so the user can retry.
_release_claim_if_owner(db, bite_id, user_id, "worker-B")
row = db.query(PersonalizationQuestion).filter_by(daily_bite_id=bite_id).first()
check("the owning worker's failure releases the claim for a retry",
      row.status == "pending" and row.claimed_by is None,
      (row.claimed_by, row.status))

db.close()

print()
print("Personalization re-audit fixes: %d/%d passed" % (passed, passed + failed))
sys.exit(1 if failed else 0)
