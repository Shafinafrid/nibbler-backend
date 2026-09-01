"""
Two-thread, two-SESSION concurrency proof for finding #5: the main nibble/
session generation claim/lease on DailyBite.

Before this fix, two concurrent POST /bites/session requests (a genuine
double-tap, or a client retrying a slow/timed-out call) for the SAME
(user_id, library_item_id, date) both ran generate_session_for_item all the
way through and both independently called the expensive LLM — only the final
`db.commit()` deduped the STORED ROW via uq_daily_bites_user_item_date's
IntegrityError, by which point the LLM had already been paid for twice.

This mirrors tests/test_personalization_lease_concurrency.py's pattern
exactly: real `threading.Thread`s, each with its OWN SQLAlchemy Session (not
one session read twice — SQLAlchemy's identity map would hide a concurrent
writer's committed state from a session that already loaded the row), and a
`threading.Barrier` held INSIDE the mocked slow LLM call so both requests are
GENUINELY racing at the exact moment the claim happens.

    .venv/bin/python tests/test_session_generation_lease_concurrency.py
"""
import os
import sys
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_session_lease_concurrency.db")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-a-real-one-000000")
os.environ.setdefault("APP_ENV", "test")

import datetime  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
from app.services import session_service as ss  # noqa: E402
from app.services.session_service import (  # noqa: E402
    generate_session_for_item, SessionGenerationError,
)

# `uq_daily_bites_user_item_date` — the unique index the whole claim
# mechanism relies on — is NOT a SQLAlchemy model-level constraint (it is a
# Postgres-style partial index applied by app/database.py's
# `_run_migrations()`, see that file's own notes on why it was applied by
# hand to production and only later backfilled here). `Base.metadata.
# create_all()` alone does NOT create it. This test must therefore create it
# explicitly — exactly the one statement from `_run_migrations()` that
# matters here, and (per that function's own docstring) valid, dialect-
# agnostic SQL that SQLite executes correctly, unlike the ADD COLUMN IF NOT
# EXISTS statements around it.

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
with engine.connect() as _conn:
    from sqlalchemy import text as _text
    _conn.execute(_text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_bites_user_item_date "
        "ON daily_bites (user_id, library_item_id, date) WHERE library_item_id IS NOT NULL"
    ))
    _conn.commit()

TODAY = datetime.date.today()


def seed(tag):
    """One Premium user with one unlocked, processed, wisdom-mode text item.
    Returns (user_id, item_id)."""
    uid = "u-" + tag + "-" + uuid.uuid4().hex[:6]
    iid = "i-" + tag + "-" + uuid.uuid4().hex[:6]
    s = SessionLocal()
    s.add(User(id=uid, email=f"{uid}@t.test", is_premium=True))
    s.add(LibraryItem(
        id=iid, user_id=uid, title="Atomic Habits", type="text",
        content="A distinctive passage about compounding small habits. " * 40,
        processed=True, mode="wisdom", is_active=True,
    ))
    s.commit()
    s.close()
    return uid, iid


def db_state(uid, iid):
    s = SessionLocal()
    try:
        rows = s.query(DailyBite).filter(
            DailyBite.user_id == uid, DailyBite.library_item_id == iid,
        ).all()
        return [{"id": r.id, "cards": bool(r.cards), "claimed_by": r.claimed_by} for r in rows]
    finally:
        s.close()


# ═════════════════════════════════════════════════════════════════════════
# Test 1 — two genuinely racing requests call the LLM (generate_wisdom_session)
# EXACTLY ONCE, and exactly one DailyBite row is ever created for the slot.
# ═════════════════════════════════════════════════════════════════════════
uid, iid = seed("race")

barrier = threading.Barrier(2, timeout=30)
released = threading.Event()
call_count = {"n": 0}
call_lock = threading.Lock()
results = {}


def _slow_generate_wisdom_session(self, **kwargs):
    """Stand-in for the real LLM call: counts every invocation, then blocks
    until BOTH racing workers are inside it, so their claims genuinely
    overlap rather than running back-to-back."""
    with call_lock:
        call_count["n"] += 1
    try:
        barrier.wait()
    except Exception:
        pass
    released.wait(timeout=10)
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


ss.LLMService.generate_wisdom_session = _slow_generate_wisdom_session


def worker(label):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        item = db.query(LibraryItem).filter(LibraryItem.id == iid).first()
        bite = generate_session_for_item(
            db, user=user, item=item, read_length=5, profile={},
            today=TODAY, origin="manual",
        )
        results[label] = {"ok": True, "bite_id": bite.id, "cards": bool(bite.cards)}
    except SessionGenerationError as e:
        results[label] = {"ok": False, "code": e.code, "status": e.status_code, "message": e.message}
    except Exception as e:
        results[label] = {"ok": False, "error": type(e).__name__, "detail": str(e)}
    finally:
        db.close()


tA = threading.Thread(target=worker, args=("A",))
tB = threading.Thread(target=worker, args=("B",))
tA.start(); tB.start()
# Release the barrier-held call shortly after both threads should have
# entered it — mirrors the personalization harness's own timer release.
threading.Timer(1.0, released.set).start()
tA.join(timeout=40)
tB.join(timeout=40)

check("both racing requests completed (no hang / deadlock)", len(results) == 2, results)
check("generate_wisdom_session (the LLM call boundary) was invoked EXACTLY ONCE across the race",
      call_count["n"] == 1, call_count)

rows = db_state(uid, iid)
check("exactly ONE DailyBite row exists for this (user, item, date) slot after the race",
      len(rows) == 1, rows)
check("that row is fully generated (cards set) and its claim is cleared",
      len(rows) == 1 and rows[0]["cards"] and rows[0]["claimed_by"] is None, rows)

succeeded = [r for r in results.values() if r.get("ok")]
check("at least one racer succeeded with a real generated session",
      len(succeeded) >= 1, results)
if len(rows) == 1:
    canonical_id = rows[0]["id"]
    check("every succeeding racer's returned bite id matches the single canonical row",
          all(r["bite_id"] == canonical_id for r in succeeded), {"canonical": canonical_id, "results": results})

losers = [(l, r) for l, r in results.items() if not r.get("ok")]
if losers:
    loser_label, loser_result = losers[0]
    check("a loser (if any) received the retryable session_generating error, not a hard failure",
          loser_result.get("code") == "session_generating" and loser_result.get("status") == 409,
          loser_result)


# ═════════════════════════════════════════════════════════════════════════
# Test 2 — a crashed/hung worker's EXPIRED claim can be taken over by a
# later request, which then succeeds (the lease is not a permanent lock).
# ═════════════════════════════════════════════════════════════════════════
uid2, iid2 = seed("expired")

# A "crashed" worker: claim the slot directly, but never finalize or release
# it, and backdate the lease so it reads as expired.
crash_db = SessionLocal()
crashed_row, won = ss._claim_or_find_daily_bite(crash_db, uid2, iid2, TODAY, "crashed-worker")
check("the crashed worker successfully claims the slot", won is True)
crashed_row.claimed_until = datetime.datetime.utcnow() - datetime.timedelta(minutes=10)
crash_db.commit()
crash_db.close()

# Restore the REAL (fast) generate_wisdom_session for this section — no
# barrier needed, this just proves forward progress, not a race.
def _fast_generate_wisdom_session(self, **kwargs):
    return {
        "title": "Recovered Session", "chapter": "Ch 1", "headline": "Headline.",
        "preview": "Preview.",
        "cards": [
            {"kind": "hook", "eyebrow": "TODAY'S SESSION", "title": "Hook",
             "body": "Body.", "highlight": None, "options": None, "explanation": None},
            {"kind": "summary", "eyebrow": "SESSION SUMMARY", "title": "Summary",
             "body": "Body.", "highlight": None, "options": None, "explanation": None},
        ],
        "quiz": None,
    }


ss.LLMService.generate_wisdom_session = _fast_generate_wisdom_session

recover_db = SessionLocal()
user2 = recover_db.query(User).filter(User.id == uid2).first()
item2 = recover_db.query(LibraryItem).filter(LibraryItem.id == iid2).first()
recovered = generate_session_for_item(
    recover_db, user=user2, item=item2, read_length=5, profile={}, today=TODAY, origin="manual",
)
recover_db.close()

check("a later request successfully re-claims an EXPIRED lease and generates a real session",
      bool(recovered.cards), {"cards": recovered.cards})
rows2 = db_state(uid2, iid2)
check("still exactly ONE row for the slot after the crashed-worker recovery (no orphaned duplicate)",
      len(rows2) == 1, rows2)
check("the recovered row is not left claimed",
      len(rows2) == 1 and rows2[0]["claimed_by"] is None, rows2)


# ═════════════════════════════════════════════════════════════════════════
# Test 3 — idempotency under client retry: a request against an
# ALREADY-COMPLETE session reuses the canonical result, never re-triggers
# the LLM.
# ═════════════════════════════════════════════════════════════════════════
call_count["n"] = 0
retry_db = SessionLocal()
user3 = retry_db.query(User).filter(User.id == uid2).first()
item3 = retry_db.query(LibraryItem).filter(LibraryItem.id == iid2).first()
retried = generate_session_for_item(
    retry_db, user=user3, item=item3, read_length=5, profile={}, today=TODAY, origin="manual",
)
retry_db.close()

check("a retry against an already-completed session returns the SAME canonical row",
      retried.id == recovered.id, {"first": recovered.id, "retry": retried.id})
check("the retry did NOT call the LLM again", call_count["n"] == 0, call_count)


print()
print("Session-generation lease concurrency: %d/%d passed" % (passed, passed + failed))
sys.exit(1 if failed else 0)
