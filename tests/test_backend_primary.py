"""
Verification for backend Primary fixes (items 4, 11, 33-index-half).

Runs against the REAL model classes and the REAL _live_unread_query, on a
throwaway SQLite file. Deliberately run from OUTSIDE nibbler-backend so its
.env (real production secrets) is never loaded — the three required settings
are supplied as env vars instead.
"""
import os
import sys
import tempfile
import datetime

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TMP = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite:///{TMP}/test.db"
os.environ["CLAUDE_API_KEY"] = "test-not-a-real-key"
os.environ["FIREBASE_PROJECT_ID"] = "test-project"
sys.path.insert(0, BACKEND)

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond:
        failures.append(name)


print("\n=== T1 — create_tables() creates every table (fix: item 11) ===")
from app.database import create_tables, engine, SessionLocal  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402

create_tables()
tables = set(inspect(engine).get_table_names())
for t in ["users", "profiles", "library_items", "daily_bites", "saved_bites",
          "streaks", "push_tokens",
          # the six that were only created by main.py's import side-effect:
          "notes", "highlights", "chat_messages", "completions",
          "user_settings", "user_state",
          "bug_reports"]:
    check(f"table {t!r} exists", t in tables)

print("\n=== T2 — unique indexes are declared, not hand-applied (fix: item 33) ===")
with engine.connect() as conn:
    idx = {r[0] for r in conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='index'"))}
check("uq_daily_bites_user_item_date created by migrations",
      "uq_daily_bites_user_item_date" in idx)
check("uq_saved_bites_user_bite created by migrations",
      "uq_saved_bites_user_bite" in idx)

print("\n=== T3 — orphaned unread bite no longer stalls the scheduler (fix: item 4) ===")
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
from app.services.notification_service import _live_unread_query  # noqa: E402

db = SessionLocal()
today = datetime.date.today()

db.add(User(id="uid-1", email="a@example.com"))
db.add(LibraryItem(id="item-live", user_id="uid-1", title="Kept Book",
                   type="pdf", processed=True, is_active=True))
db.add(LibraryItem(id="item-doomed", user_id="uid-1", title="Book To Delete",
                   type="pdf", processed=True, is_active=True))
db.commit()

# One unread bite per book.
db.add(DailyBite(id="bite-live", user_id="uid-1", library_item_id="item-live",
                 title="live", insight="i", reflection="r", action="a",
                 date=today, read_at=None))
db.add(DailyBite(id="bite-orphan", user_id="uid-1", library_item_id="item-doomed",
                 title="orphan", insight="i", reflection="r", action="a",
                 date=today - datetime.timedelta(days=3), read_at=None))
db.commit()


def old_query_count():
    """The pre-fix query, reproduced verbatim for comparison."""
    return (db.query(DailyBite)
            .filter(DailyBite.user_id == "uid-1", DailyBite.read_at.is_(None))
            .count())


def new_query_count():
    return _live_unread_query(db, "uid-1").count()


check("baseline: both queries see both unread bites",
      old_query_count() == 2 and new_query_count() == 2,
      f"old={old_query_count()} new={new_query_count()}")

# The user deletes the book they didn't like — exactly what DELETE /library/{id}
# does today: the item row goes, its daily_bites do NOT (no FK on bite.py:19).
db.query(LibraryItem).filter(LibraryItem.id == "item-doomed").delete()
db.commit()

check("orphan row really does survive the delete (no FK)",
      db.query(DailyBite).filter(DailyBite.id == "bite-orphan").first() is not None)
check("OLD query still sees the unreachable orphan → permanent stall",
      old_query_count() == 2, f"old={old_query_count()}")
check("NEW query ignores the orphan",
      new_query_count() == 1, f"new={new_query_count()}")

# Now the real point: with the live bite read, generation must be free to run.
db.query(DailyBite).filter(DailyBite.id == "bite-live").update(
    {"read_at": datetime.datetime.utcnow()})
db.commit()
check("OLD query STILL blocks generation forever (the bug)",
      old_query_count() == 1, f"old={old_query_count()}")
check("NEW query is clear → scheduler can generate again (the fix)",
      new_query_count() == 0, f"new={new_query_count()}")

print("\n=== T4 — the legitimate hold still works (no false negative) ===")
# NB: a different date from bite-live — uq_daily_bites_user_item_date (added to
# _run_migrations by the item-33 fix) correctly rejects a second row for the
# same (user, item, date), which this test hit on the first run.
db.add(DailyBite(id="bite-held", user_id="uid-1", library_item_id="item-live",
                 title="held", insight="i", reflection="r", action="a",
                 date=today - datetime.timedelta(days=1), read_at=None))
db.commit()
check("an unread bite on an EXISTING book still holds generation",
      new_query_count() == 1, f"new={new_query_count()}")

# A deactivated book must still hold — the user can open it from the Library.
db.query(LibraryItem).filter(LibraryItem.id == "item-live").update({"is_active": False})
db.commit()
check("a DEACTIVATED book's unread bite still holds (deliberate)",
      new_query_count() == 1, f"new={new_query_count()}")

print("\n=== T5 — another user's item can never satisfy the join ===")
db.add(User(id="uid-2", email="b@example.com"))
db.add(LibraryItem(id="item-other", user_id="uid-2", title="Someone Else's",
                   type="pdf", processed=True, is_active=True))
db.add(DailyBite(id="bite-cross", user_id="uid-1", library_item_id="item-other",
                 title="cross", insight="i", reflection="r", action="a",
                 date=today, read_at=None))
db.commit()
check("a bite pointing at ANOTHER user's item is not counted",
      new_query_count() == 1, f"new={new_query_count()}")

db.close()

print("\n" + "=" * 62)
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: all backend primary-fix checks passed")
