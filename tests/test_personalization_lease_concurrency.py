"""
Two-SESSION concurrency tests for the personalization answer lease.

Round-3's lease test hand-edited ownership inside ONE session and then
computed the intended comparison — which is exactly why it passed while the
guard did not work. SQLAlchemy's identity map returns the CACHED object when
a session re-queries a row it has already loaded, so the superseded worker's
ownership check compared against its own stale in-memory state and passed.

These tests use genuinely separate Sessions, the way two concurrent requests
do, and assert the DATABASE's final state rather than any in-memory view.
"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_lease_concurrency.db")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")

from datetime import datetime, timedelta

from app.database import Base, engine, SessionLocal
from app.models.personalization import PersonalizationQuestion
from app.routers.bites import (
    _claim_personalization_row, _finalize_personalization_answer,
    _release_claim_if_owner,
)

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


Base.metadata.create_all(bind=engine)


def seed():
    """One pending question, returned as (bite_id, user_id)."""
    bite_id = "bite-" + uuid.uuid4().hex[:8]
    user_id = "user-" + uuid.uuid4().hex[:8]
    s = SessionLocal()
    s.add(PersonalizationQuestion(
        user_id=user_id, daily_bite_id=bite_id, question="Q?",
        options=[{"id": "opt0", "text": "A", "tag": "prefers_automation"}],
        profile_id="prof-1", status="pending",
    ))
    s.commit()
    s.close()
    return bite_id, user_id


def db_state(bite_id):
    """Read committed state through a FRESH session — never one that has
    already loaded this row, or the identity map hides the truth."""
    s = SessionLocal()
    try:
        r = s.query(PersonalizationQuestion).filter_by(daily_bite_id=bite_id).first()
        return {"status": r.status, "claimed_by": r.claimed_by,
                "tags": list(r.applied_tags or []), "free_text": r.answer_free_text}
    finally:
        s.close()


# ── A superseded worker must not finalize over the worker that took over ──
{}
bite_id, user_id = seed()

sA = SessionLocal()
sB = SessionLocal()

# A claims, loading the row into A's identity map (as the real handler does
# before its LLM call).
claim_a = _claim_personalization_row(sA, bite_id, user_id, "worker-A")
check("worker A takes the initial claim", claim_a is True, claim_a)

# A's lease expires; B legitimately takes over in its own session.
expire = SessionLocal()
row = expire.query(PersonalizationQuestion).filter_by(daily_bite_id=bite_id).first()
row.claimed_until = datetime.utcnow() - timedelta(minutes=1)
expire.commit()
expire.close()

claim_b = _claim_personalization_row(sB, bite_id, user_id, "worker-B")
check("worker B can take over an expired lease", claim_b is True, claim_b)
check("the database now records B as the owner",
      db_state(bite_id)["claimed_by"] == "worker-B", db_state(bite_id))

# A now finishes late, in ITS OWN session, which still has the stale row
# cached. This is the exact path round 3 got wrong.
ok_a = _finalize_personalization_answer(
    sA, bite_id, user_id, "worker-A",
    tags=["prefers_automation"], interpreted_summary=None,
    option_id=None, free_text="A's answer",
)
check("a SUPERSEDED worker's finalize is refused (fresh DB state, not the "
      "session's cached row)", ok_a is False, ok_a)

after_a = db_state(bite_id)
check("the database was NOT overwritten by the superseded worker",
      after_a["free_text"] != "A's answer", after_a)
check("the live owner's claim survives the superseded worker's attempt",
      after_a["claimed_by"] == "worker-B", after_a)

# B, the real owner, finalizes successfully.
ok_b = _finalize_personalization_answer(
    sB, bite_id, user_id, "worker-B",
    tags=["prefers_automation"], interpreted_summary=None,
    option_id=None, free_text="B's answer",
)
check("the OWNING worker's finalize succeeds", ok_b is True, ok_b)

final = db_state(bite_id)
check("the database records the owning worker's answer",
      final["free_text"] == "B's answer", final)
check("the row is marked answered", final["status"] == "answered", final)
check("the claim is cleared on success", final["claimed_by"] is None, final)

sA.close()
sB.close()


# ── A superseded worker's FAILURE must not release the live owner's claim ─
bite_id2, user_id2 = seed()
sC = SessionLocal()
sD = SessionLocal()

_claim_personalization_row(sC, bite_id2, user_id2, "worker-C")
expire = SessionLocal()
row = expire.query(PersonalizationQuestion).filter_by(daily_bite_id=bite_id2).first()
row.claimed_until = datetime.utcnow() - timedelta(minutes=1)
expire.commit()
expire.close()
_claim_personalization_row(sD, bite_id2, user_id2, "worker-D")

# C fails and tries to release — in its own session, with a stale cached row.
_release_claim_if_owner(sC, bite_id2, user_id2, "worker-C")
state = db_state(bite_id2)
check("a superseded worker's failure does NOT reset the live claim",
      state["status"] == "processing" and state["claimed_by"] == "worker-D", state)

# D fails and releases legitimately.
_release_claim_if_owner(sD, bite_id2, user_id2, "worker-D")
state = db_state(bite_id2)
check("the owning worker's failure DOES release the claim for a retry",
      state["status"] == "pending" and state["claimed_by"] is None, state)

sC.close()
sD.close()


# ── A live (unexpired) claim blocks a second claimant outright ────────────
bite_id3, user_id3 = seed()
sE = SessionLocal()
sF = SessionLocal()
check("first claimant on a fresh row succeeds",
      _claim_personalization_row(sE, bite_id3, user_id3, "worker-E") is True)
check("a second claimant is refused while the first lease is LIVE",
      _claim_personalization_row(sF, bite_id3, user_id3, "worker-F") is False)
check("the database still records the first claimant",
      db_state(bite_id3)["claimed_by"] == "worker-E", db_state(bite_id3))
sE.close()
sF.close()


# ── Finalizing an already-answered row never double-writes ───────────────
bite_id4, user_id4 = seed()
sG = SessionLocal()
_claim_personalization_row(sG, bite_id4, user_id4, "worker-G")
_finalize_personalization_answer(sG, bite_id4, user_id4, "worker-G",
                                 tags=["shift_practical"], interpreted_summary=None,
                                 option_id="opt0", free_text=None)
sH = SessionLocal()
ok_h = _finalize_personalization_answer(sH, bite_id4, user_id4, "worker-H",
                                        tags=["decrease_confidence"], interpreted_summary=None,
                                        option_id=None, free_text="late")
check("finalizing an already-answered row is refused", ok_h is False, ok_h)
check("the first answer's tags are preserved",
      db_state(bite_id4)["tags"] == ["shift_practical"], db_state(bite_id4))
sG.close()
sH.close()


print()
print("Personalization lease concurrency: %d/%d passed" % (passed, passed + failed))
sys.exit(1 if failed else 0)
