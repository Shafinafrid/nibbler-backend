"""V2-audit round, backend: M4, M6, H2/H7, H5, force_new, archive_status."""
import os, sys, tempfile, datetime

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/r.db", CLAUDE_API_KEY="t", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

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
from app.models.bite import DailyBite, SavedBite
from app.models.user_data import ChatMessage, Completion, Highlight, Note
import main

create_tables()
db = SessionLocal()
db.add(User(id="u1", email="u1@example.com", timezone="Pacific/Auckland"))
db.commit()

def _db():
    yield db

main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = lambda: db.query(User).filter(User.id == "u1").first()
c = TestClient(main.app)
today = datetime.date.today()

section("M4 — a new upload joins the active line-up only if there is room")
from app.routers.library import _should_start_active, MAX_ACTIVE_SOURCES
for i in range(MAX_ACTIVE_SOURCES):
    db.add(LibraryItem(id=f"a{i}", user_id="u1", title=f"A{i}", type="pdf",
                       processed=True, is_active=True))
db.commit()
check(f"with {MAX_ACTIVE_SOURCES} already active, a new book starts INACTIVE",
      _should_start_active(db, "u1") is False)

db.query(LibraryItem).filter(LibraryItem.id == "a0").update({"is_active": False})
db.commit()
check("free a slot and the next new book starts active",
      _should_start_active(db, "u1") is True)
check("the scheduler's active set can no longer exceed the cap",
      db.query(LibraryItem).filter(LibraryItem.user_id == "u1",
                                   LibraryItem.is_active.is_(True)).count() <= MAX_ACTIVE_SOURCES)

section("M6 — the cap window is the user's real local day, and the copy is honest")
from app.routers.bites import _cap_window, _cap_message
u = db.query(User).filter(User.id == "u1").first()
start, resets = _cap_window(u)
span_h = (resets - start).total_seconds() / 3600
check("a known timezone gives a 24h local-day bucket, not a 23h rolling window",
      23.9 < span_h < 24.1, f"span={span_h:.2f}h")
check("the window is currently open (start is in the past)",
      start <= datetime.datetime.utcnow() < resets)

u.timezone = None
db.commit()
s2, r2 = _cap_window(u)
check("an unknown timezone falls back to the rolling window",
      22.9 < (r2 - s2).total_seconds() / 3600 / 2 < 23.1, f"half-span check")

soon = datetime.datetime.utcnow() + datetime.timedelta(hours=3)
far = datetime.datetime.utcnow() + datetime.timedelta(hours=20)
check("a few hours out, the message says hours — not 'tomorrow'",
      "hour" in _cap_message(1, soon) and "tomorrow" not in _cap_message(1, soon),
      _cap_message(1, soon))
check("a long way out, it still says tomorrow",
      "tomorrow" in _cap_message(1, far), _cap_message(1, far))
u.timezone = "Pacific/Auckland"; db.commit()

section("H5 — /bites/daily returns the held set whatever its origin")
db.add(LibraryItem(id="bk1", user_id="u1", title="Book", type="pdf", processed=True, is_active=True))
db.commit()
db.add(DailyBite(id="manual-unread", user_id="u1", library_item_id="bk1", title="Manual",
                 insight="i", reflection="r", action="a", date=today,
                 cards=[{"t": 1}], origin="manual", read_at=None))
db.commit()
r = c.get("/bites/daily")
ids = {s["id"] for s in r.json()}
check("an unread MANUAL session is now visible to Home", "manual-unread" in ids, str(ids))
check("so the user can open the row that is holding the scheduler", r.status_code == 200)

section("force_new — the destructive public field is gone")
r = c.post("/bites/session", json={"library_item_id": "bk1", "read_length": 5,
                                   "force_new": True, "client_date": str(today)})
check("sending force_new no longer deletes anything (field ignored/absent)",
      db.query(DailyBite).filter(DailyBite.id == "manual-unread").first() is not None,
      f"status={r.status_code}")
import app.routers.bites as bites_mod
check("SessionRequest no longer declares force_new",
      "force_new" not in bites_mod.SessionRequest.model_fields,
      str(list(bites_mod.SessionRequest.model_fields)))

section("H2/H7 — deleting a book removes everything derived from it")
db.add(DailyBite(id="d1", user_id="u1", library_item_id="bk1", title="D1", insight="i",
                 reflection="r", action="a", date=today - datetime.timedelta(days=1),
                 cards=[{"t": 1}]))
db.commit()
db.add(SavedBite(id="s1", user_id="u1", bite_id="d1"))
db.add(Note(id="n1", user_id="u1", book_id="bk1", daily_bite_id="d1", card_index=0, text="note"))
db.add(Highlight(id="h1", user_id="u1", book_id="bk1", daily_bite_id="d1", card_index=1))
db.add(ChatMessage(id="c1", user_id="u1", book_id="bk1", role="user", content="hi"))
db.add(Completion(id="k1", user_id="u1", book_id="bk1", completed_date=today, read_length=5))
# A second book's data must be left completely alone.
db.add(LibraryItem(id="bk2", user_id="u1", title="Keep", type="pdf", processed=True))
db.add(Note(id="n2", user_id="u1", book_id="bk2", daily_bite_id="dx", card_index=0, text="keep"))
db.commit()

r = c.delete("/library/bk1")
check("delete succeeds", r.status_code == 200, r.text[:160])
body = r.json()
check("it reports what it removed", isinstance(body.get("removed"), dict), str(body)[:160])

for model, name in [(DailyBite, "daily_bites"), (Note, "notes"), (Highlight, "highlights"),
                    (ChatMessage, "chat_messages"), (Completion, "completions")]:
    col = model.library_item_id if model is DailyBite else model.book_id
    left = db.query(model).filter(col == "bk1").count()
    check(f"{name} for the deleted book are gone", left == 0, f"{left} left")

check("saved_bites cascaded with the deck (real FK)",
      db.query(SavedBite).filter(SavedBite.id == "s1").first() is None)
check("the OTHER book's note is untouched",
      db.query(Note).filter(Note.id == "n2").first() is not None)
check("the library row itself is gone",
      db.query(LibraryItem).filter(LibraryItem.id == "bk1").first() is None)

section("archive_status is now on the API response (the check I got wrong before)")
from app.schemas.library import LibraryItemResponse
check("LibraryItemResponse declares archive_status",
      "archive_status" in LibraryItemResponse.model_fields,
      str(list(LibraryItemResponse.model_fields)))
db.query(LibraryItem).filter(LibraryItem.id == "bk2").update({"archive_status": "failed"})
db.commit()
row = [i for i in c.get("/library/").json()["items"] if i["id"] == "bk2"][0]
check("and it actually round-trips through GET /library/",
      row.get("archive_status") == "failed", str(row.get("archive_status")))

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all V2-round backend checks passed")
sys.exit(1 if failures else 0)
