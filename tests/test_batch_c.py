"""Verification for Batch C backend (items 3/27, 28, 35, 30, 8/29/39)."""
import os, sys, tempfile, datetime

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/d.db", CLAUDE_API_KEY="t", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
# Isolate from the real .env, which holds production AWS/Pinecone/Voyage
# credentials. Setting env vars is not enough: `env_file = ".env"` is a
# RELATIVE path, so every key NOT overridden here still came from the real
# file — which is how this suite once made a live S3 request. hermetic.py
# moves the process somewhere .env does not exist. Must precede `app.` imports.
import hermetic  # noqa: F401

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
from app.models.streak import Streak
from app.models.user_data import Completion
import main

create_tables()
db = SessionLocal()
db.add(User(id="u1", email="u1@example.com"))
db.add(LibraryItem(id="bk1", user_id="u1", title="Book One", type="pdf", processed=True))
db.commit()

def _db():
    # Share ONE session with the test. In production get_current_user resolves
    # its User through Depends(get_db), so the user and the request session are
    # always the same one — handing the endpoint a different session makes
    # _get_or_create_profile's db.refresh(user) fail on a harness artifact
    # rather than a real defect.
    yield db

main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = lambda: db.query(User).filter(User.id == "u1").first()
c = TestClient(main.app)

today = datetime.date.today()
db.add(DailyBite(id="bite-1", user_id="u1", library_item_id="bk1", title="Session One",
                 insight="i", reflection="r", action="a", date=today,
                 cards=[{"title": "Card 1", "body": "b"}],
                 quiz=[{"question": "Q?", "options": [{"text": "A", "correct": True}]}],
                 mode="wisdom", read_length=5))
db.commit()

section("ITEM 3/27 — one durable, idempotent session completion")
payload = {"id": "cmp-1", "book_id": "bk1", "daily_bite_id": "bite-1",
           "completed_date": str(today), "read_length": 5}
r = c.post("/sync/session-complete", json=payload)
check("first call succeeds", r.status_code == 200, r.text[:200])
body = r.json()
check("it was applied (not a replay)", body["already_applied"] is False, str(body))
check("the nibble was marked read", body["bite_marked_read"] is True, str(body))
check("the streak moved to 1", body["current_streak"] == 1, str(body))

db.expire_all()
bite = db.query(DailyBite).filter(DailyBite.id == "bite-1").first()
check("read_at is actually set — the scheduler's hold is released",
      bite.read_at is not None)
check("a completion row exists",
      db.query(Completion).filter(Completion.id == "cmp-1").first() is not None)

section("ITEM 3/27 — replaying it (the outbox retries every foreground) is safe")
r2 = c.post("/sync/session-complete", json=payload)
check("replay reports already_applied", r2.json()["already_applied"] is True, r2.text[:200])
check("the streak did NOT double-count",
      r2.json()["current_streak"] == 1, str(r2.json()))
check("still exactly one completion row",
      db.query(Completion).filter(Completion.id == "cmp-1").count() == 1)
db.expire_all()
streak = db.query(Streak).filter(Streak.user_id == "u1").first()
check("total_bites_read counted once", streak.total_bites_read == 1,
      f"total={streak.total_bites_read}")

section("ITEM 3/27 — a completion with no bite id still works (demo/legacy sessions)")
r = c.post("/sync/session-complete", json={
    "id": "cmp-2", "book_id": "bk1", "completed_date": str(today), "read_length": 5})
check("accepted without daily_bite_id", r.status_code == 200, r.text[:160])
check("reports that no bite was marked", r.json()["bite_marked_read"] is False, str(r.json()))

section("ITEM 3/27 — cannot mark another user's nibble read")
db.add(User(id="u2", email="u2@example.com"))
db.add(LibraryItem(id="bk2", user_id="u2", title="Theirs", type="pdf", processed=True))
db.commit()
db.add(DailyBite(id="bite-theirs", user_id="u2", library_item_id="bk2", title="T",
                 insight="i", reflection="r", action="a", date=today, cards=[{"a": 1}]))
db.commit()
c.post("/sync/session-complete", json={
    "id": "cmp-3", "book_id": "bk2", "daily_bite_id": "bite-theirs",
    "completed_date": str(today), "read_length": 5})
db.expire_all()
check("the other user's bite is untouched",
      db.query(DailyBite).filter(DailyBite.id == "bite-theirs").first().read_at is None)

section("ITEM 28 — past sessions come back WITH their decks and quizzes")
r = c.get("/bites/sessions")
check("endpoint responds", r.status_code == 200, r.text[:200])
sessions = r.json()["sessions"]
check("the session is returned", len(sessions) == 1, f"n={len(sessions)}")
s0 = sessions[0]
check("cards are included", bool(s0.get("cards")), str(s0.get("cards"))[:80])
check("quiz is included — this is what Review rebuilds from",
      bool(s0.get("quiz")), str(s0.get("quiz"))[:80])
check("library_item_id + date are present (the cache key)",
      s0.get("library_item_id") == "bk1" and s0.get("date") == str(today), str(s0)[:120])

# Contrast with the old endpoint, which is why Review could not rebuild.
old = c.get("/bites/history").json()["bites"]
check("OLD /bites/history still omits cards and quiz (the original gap)",
      old and "cards" not in old[0] and "quiz" not in old[0], str(old[0])[:120])

section("ITEM 28 — decks with no cards are not shipped")
db.add(DailyBite(id="bite-empty", user_id="u1", library_item_id="bk1", title="Empty",
                 insight="i", reflection="r", action="a",
                 date=today - datetime.timedelta(days=1), cards=[]))
db.commit()
ids = {s["id"] for s in c.get("/bites/sessions").json()["sessions"]}
check("empty deck excluded", "bite-empty" not in ids, str(ids))

section("ITEM 35 — a stale growth profile cannot overwrite a newer one")
newer = {"person": {"name": "Real"}, "profiles": [{"id": "p1"}],
         "activeProfileId": "p1", "updatedAt": "2026-07-26T12:00:00.000Z"}
r = c.put("/profile/growth", json={"growth_state": newer})
check("newer profile is stored", r.status_code == 200, r.text[:160])

older = {"person": {"name": "Stale"}, "profiles": [{"id": "p0"}],
         "activeProfileId": "p0", "updatedAt": "2026-07-20T09:00:00.000Z"}
r = c.put("/profile/growth", json={"growth_state": older})
check("an older push is accepted as a no-op, not an error", r.status_code == 200, r.text[:160])
check("the NEWER profile survived",
      r.json()["growth_state"]["person"]["name"] == "Real",
      r.json()["growth_state"]["person"]["name"])

newest = {"person": {"name": "Newest"}, "profiles": [{"id": "p2"}],
          "activeProfileId": "p2", "updatedAt": "2026-07-27T08:00:00.000Z"}
r = c.put("/profile/growth", json={"growth_state": newest})
check("a genuinely newer push still applies",
      r.json()["growth_state"]["person"]["name"] == "Newest",
      r.json()["growth_state"]["person"]["name"])

# An old client sends no timestamp at all — must keep working.
r = c.put("/profile/growth", json={"growth_state":
          {"person": {"name": "NoStamp"}, "profiles": [{"id": "p3"}], "activeProfileId": "p3"}})
check("an unstamped push from an older client still applies",
      r.json()["growth_state"]["person"]["name"] == "NoStamp",
      r.json()["growth_state"]["person"]["name"])

# The empty-profile guard must still fire.
r = c.put("/profile/growth", json={"growth_state": {"person": {}, "profiles": []}})
check("the empty-profile guard still returns 409", r.status_code == 409, str(r.status_code))

section("ITEM 30 — deletion helpers report failure instead of swallowing it")
from app.services.s3_service import S3Service
from app.services.embedding_service import EmbeddingService
import inspect as _inspect
check("S3Service.delete_file returns a value",
      "-> bool" in _inspect.getsource(S3Service.delete_file))
check("delete_user_namespace returns a value",
      "-> bool" in _inspect.getsource(EmbeddingService.delete_user_namespace))

svc = EmbeddingService.__new__(EmbeddingService)
svc.pinecone_available = False
check("no Pinecone configured counts as a real success (nothing orphaned)",
      svc.delete_user_namespace("u1") is True)

class _Boom:
    def delete(self, **kw): raise RuntimeError("pinecone down")
svc.pinecone_available = True
svc.index = _Boom()
check("a genuine Pinecone failure returns False", svc.delete_user_namespace("u1") is False)

# Production bug (Aug 2026): a namespace that was never created (account
# never uploaded/embedded anything — common for free/trial/test accounts)
# made Pinecone's delete_all raise a 404 NotFoundException, which the
# broad except was treating identically to a real failure — permanently
# stranding every such account's erasure in retry forever (reproduced live:
# 23 retries, 'vectors' as the sole failing class, on an account that had
# never uploaded a book).
from pinecone.exceptions import NotFoundException
class _NeverCreated:
    def delete(self, **kw): raise NotFoundException(status=404, reason="Not Found")
svc.index = _NeverCreated()
check("a namespace that never existed (404) counts as success, not failure — "
      "same end state as one just emptied",
      svc.delete_user_namespace("u1") is True)

from app.routers.auth import delete_account, _attempt_account_erasure_cleanup
src = _inspect.getsource(delete_account)
cleanup_src = _inspect.getsource(_attempt_account_erasure_cleanup)
# Task 2 closeout (Verified Blocker 8) replaced the old single-pass endpoint
# with a durable erasure state machine: the response now reports a single
# truthful `complete` flag rather than a per-system `erased` breakdown (the
# old shape), and per-subsystem failures are logged individually (below)
# rather than behind one generic "ERASURE INCOMPLETE" line.
check("the endpoint reports a truthful complete flag on both outcomes",
      '"complete": True' in src and '"complete": False' in src)
check("logs loudly, per subsystem, when a piece of erasure fails",
      cleanup_src.count('logger.error("Erasure:') >= 3)

section("ITEM 8/29/39 — archive status is recorded, not conflated with `processed`")
from sqlalchemy import inspect as sa_inspect
from app.database import engine
cols = {col["name"] for col in sa_inspect(engine).get_columns("library_items")}
check("library_items.archive_status exists", "archive_status" in cols)
lib_src = open(f"{BACKEND}/app/routers/library.py").read()
# Blocker 2 moved this into an atomic-ownership-write lambda (setattr, not a
# bare attribute assignment) to close a real race between the upload and a
# concurrent ownership change — same effect, different call shape.
check("archival success is recorded",
      '"archive_status", "stored")' in lib_src)
check("archival failure is recorded rather than only printed",
      'archive_status = "failed"' in lib_src and "S3 archive FAILED" in lib_src)

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Batch C backend checks passed")
sys.exit(1 if failures else 0)
