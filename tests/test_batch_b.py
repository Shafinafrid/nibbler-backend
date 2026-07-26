"""Verification for Batch B (item 21 note/highlight identity + 5/36 delete-by-key)."""
import os, sys, tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/c.db", CLAUDE_API_KEY="t", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from app.database import create_tables, SessionLocal, get_db, engine
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.user_data import Note, Highlight
import main

create_tables()
db = SessionLocal()
db.add(User(id="u1", email="u1@example.com"))
db.commit()

def _db():
    d = SessionLocal()
    try: yield d
    finally: d.close()

main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = lambda: db.query(User).filter(User.id == "u1").first()
c = TestClient(main.app)

section("SCHEMA — the old unconditional key is gone, two partial ones replace it")
idx = {r[0] for r in engine.connect().execute(
    text("SELECT name FROM sqlite_master WHERE type='index'"))}
cols = {col["name"] for col in inspect(engine).get_columns("notes")}
check("notes.daily_bite_id column exists", "daily_bite_id" in cols)
check("OLD uq_notes_user_book_card is NOT present", "uq_notes_user_book_card" not in idx,
      f"present={'uq_notes_user_book_card' in idx}")
for name in ["uq_notes_user_bite_card", "uq_notes_user_book_card_legacy",
             "uq_highlights_user_bite_card", "uq_highlights_user_book_card_legacy"]:
    check(f"{name} created", name in idx)

section("ITEM 21 — two sessions of ONE book can each hold a note on card 0")
r1 = c.put("/sync/notes", json={"book_id": "bk1", "daily_bite_id": "bite-mon",
                                "card_index": 0, "text": "Monday's thought"})
r2 = c.put("/sync/notes", json={"book_id": "bk1", "daily_bite_id": "bite-tue",
                                "card_index": 0, "text": "Tuesday's thought"})
check("both writes succeed", r1.status_code == 200 and r2.status_code == 200,
      f"{r1.status_code}/{r2.status_code}")
notes = [n for n in c.get("/sync/all").json()["notes"] if n["book_id"] == "bk1"]
check("TWO rows survive — the collision is gone", len(notes) == 2, f"rows={len(notes)}")
texts = sorted(n["text"] for n in notes)
check("Monday's note was NOT overwritten by Tuesday's",
      texts == ["Monday's thought", "Tuesday's thought"], str(texts))

section("ITEM 21 — re-pushing the SAME card of the SAME session still updates")
c.put("/sync/notes", json={"book_id": "bk1", "daily_bite_id": "bite-mon",
                           "card_index": 0, "text": "Monday, revised"})
notes = [n for n in c.get("/sync/all").json()["notes"] if n["book_id"] == "bk1"]
check("still two rows (no duplicate from the retry path)", len(notes) == 2, f"rows={len(notes)}")
mon = [n for n in notes if n["daily_bite_id"] == "bite-mon"]
check("the Monday row was updated in place", len(mon) == 1 and mon[0]["text"] == "Monday, revised",
      str([n["text"] for n in mon]))

section("ITEM 21 — highlights behave the same way")
c.put("/sync/highlights", json={"book_id": "bk2", "daily_bite_id": "b-a", "card_index": 0,
                                "card_title": "Card A"})
c.put("/sync/highlights", json={"book_id": "bk2", "daily_bite_id": "b-b", "card_index": 0,
                                "card_title": "Card B"})
hls = [h for h in c.get("/sync/all").json()["highlights"] if h["book_id"] == "bk2"]
check("two sessions each keep their own highlight on card 0", len(hls) == 2, f"rows={len(hls)}")

section("LEGACY — pre-existing rows (daily_bite_id NULL) are untouched and still work")
# Simulate a row written before this change, exactly as the old code stored it.
db.add(Note(id="legacy-1", user_id="u1", book_id="bk9", card_index=0,
            text="written before the fix"))
db.commit()
legacy = [n for n in c.get("/sync/all").json()["notes"] if n["book_id"] == "bk9"]
check("the legacy row is still returned by /sync/all", len(legacy) == 1)
check("its daily_bite_id is null", legacy and legacy[0]["daily_bite_id"] is None)

# An OLD client (no daily_bite_id) re-pushing must still update it in place.
c.put("/sync/notes", json={"book_id": "bk9", "card_index": 0, "text": "old client edit"})
legacy = [n for n in c.get("/sync/all").json()["notes"] if n["book_id"] == "bk9"]
check("an old client's re-push updates the legacy row, not duplicates it",
      len(legacy) == 1 and legacy[0]["text"] == "old client edit",
      f"rows={len(legacy)}")

# A NEW session-scoped note on the same book+card must NOT collide with it.
c.put("/sync/notes", json={"book_id": "bk9", "daily_bite_id": "bite-new",
                           "card_index": 0, "text": "new session note"})
legacy = [n for n in c.get("/sync/all").json()["notes"] if n["book_id"] == "bk9"]
check("a session-scoped note coexists with the legacy row", len(legacy) == 2, f"rows={len(legacy)}")
check("the legacy row's text is intact",
      any(n["text"] == "old client edit" and n["daily_bite_id"] is None for n in legacy))

section("ITEM 5/36 — delete finds the row even when the id was minted elsewhere")
r = c.put("/sync/notes", json={"book_id": "bkX", "daily_bite_id": "bite-x",
                               "card_index": 2, "text": "device A's note"})
server_id = r.json()["id"]
# Device B holds a DIFFERENT id for the same logical row.
r = c.delete("/sync/notes/some-other-device-id"
             "?book_id=bkX&card_index=2&daily_bite_id=bite-x")
check("delete reports it actually removed something", r.json().get("deleted") is True, r.text)
check("the row is really gone (no resurrection on restore)",
      not [n for n in c.get("/sync/all").json()["notes"] if n["book_id"] == "bkX"])

# Deleting by the correct id still works (old clients send only that).
r = c.put("/sync/notes", json={"book_id": "bkY", "card_index": 0, "text": "by id"})
r = c.delete(f"/sync/notes/{r.json()['id']}")
check("delete by id alone still works (old clients unaffected)",
      r.json().get("deleted") is True, r.text)

# A delete that matches nothing must stay harmless.
r = c.delete("/sync/notes/nope?book_id=ghost&card_index=99&daily_bite_id=none")
check("a delete matching nothing is a harmless no-op",
      r.status_code == 200 and r.json().get("deleted") is False, r.text)

section("SAFETY — a delete cannot reach another user's row")
db.add(User(id="u2", email="u2@example.com"))
db.add(Note(id="u2-note", user_id="u2", book_id="bkZ", daily_bite_id="bz",
            card_index=0, text="not yours"))
db.commit()
c.delete("/sync/notes/u2-note?book_id=bkZ&card_index=0&daily_bite_id=bz")
check("another user's note survives a matching natural-key delete",
      db.query(Note).filter(Note.id == "u2-note").first() is not None)

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Batch B checks passed")
sys.exit(1 if failures else 0)
