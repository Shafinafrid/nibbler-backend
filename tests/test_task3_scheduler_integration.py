"""
Task 3 — the REAL scheduled-push functions (_notify_delivery_slot,
_notify_streak_alert_slot), not just preview_notification_for_user through
the HTTP test-button endpoint. These are what actually runs every 5 minutes
in production; the on-demand button shares their copy-building helpers but
never exercises their own multi-user token-grouping/batching logic, which
changed structurally for Task 3 (one shared title/body per BATCH became one
message PER USER — see send_push_messages). That surface has no other test,
so this proves it directly against a real SQLite DB with two DIFFERENT
users, each with their own book, in the same delivery slot.

    .venv/bin/python tests/test_task3_scheduler_integration.py
"""

import asyncio
import datetime
import os
import sys
import tempfile
import uuid

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/sched.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from app.database import create_tables, SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.push_token import PushToken  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
from app.models.streak import Streak  # noqa: E402
import app.services.notification_service as notif_service  # noqa: E402

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

create_tables()

def db_factory():
    return SessionLocal()

# ── stub the actual Expo call, record what would have gone out ──────────────
sent_batches = []
async def _fake_send_messages(messages, expo_access_token=""):
    sent_batches.append(list(messages))
    return [{"status": "ok"} for _ in messages]
notif_service.send_push_messages = _fake_send_messages

TODAY = datetime.date.today()
YDAY = TODAY - datetime.timedelta(days=1)
NOW = datetime.datetime.combine(TODAY, datetime.time(9, 0))

# ─────────────────────────────────────────────────────────────────────────
section("_notify_delivery_slot — two DIFFERENT users, two DIFFERENT books, one tick")
# ─────────────────────────────────────────────────────────────────────────
db = SessionLocal()
db.add_all([
    User(id="userA", email="a@example.com"),
    User(id="userB", email="b@example.com"),
])
db.add_all([
    LibraryItem(id="bookA", user_id="userA", title="Atomic Habits", type="pdf", processed=True),
    LibraryItem(id="bookB", user_id="userB", title="Deep Work", type="pdf", processed=True),
])
db.add_all([
    DailyBite(id="biteA", user_id="userA", title="t", insight="i", reflection="r", action="a",
              date=TODAY, library_item_id="bookA", headline="The 1% rule."),
    DailyBite(id="biteB", user_id="userB", title="t", insight="i", reflection="r", action="a",
              date=TODAY, library_item_id="bookB", headline="Protect your deep hours."),
])
db.add_all([
    PushToken(id=str(uuid.uuid4()), user_id="userA", token="ExponentPushToken[A]",
              notification_hour=9, notification_minute=0),
    PushToken(id=str(uuid.uuid4()), user_id="userB", token="ExponentPushToken[B]",
              notification_hour=9, notification_minute=0),
])
db.commit()
db.close()

sent_batches.clear()
asyncio.run(notif_service._notify_delivery_slot(db_factory, NOW))

check("exactly one batched call to Expo for this tick", len(sent_batches) == 1, len(sent_batches))
batch = sent_batches[0] if sent_batches else []
check("one message per user, not merged/deduplicated away", len(batch) == 2, len(batch))

by_token = {m["to"]: m for m in batch}
msgA = by_token.get("ExponentPushToken[A]")
msgB = by_token.get("ExponentPushToken[B]")
check("user A's push names user A's OWN book", msgA and msgA["title"] == "Atomic Habits", msgA)
check("user A's push carries user A's own headline", msgA and msgA["body"] == "The 1% rule.", msgA)
check("user A's payload deep-links to user A's own book/bite",
      msgA and msgA["data"] == {"screen": "Session", "bookId": "bookA", "biteId": "biteA"}, msgA)
check("user B's push names user B's OWN book — not user A's (no cross-talk)",
      msgB and msgB["title"] == "Deep Work", msgB)
check("user B's push carries user B's own headline — not user A's",
      msgB and msgB["body"] == "Protect your deep hours.", msgB)
check("user B's payload deep-links to user B's own book/bite",
      msgB and msgB["data"] == {"screen": "Session", "bookId": "bookB", "biteId": "biteB"}, msgB)

# ─────────────────────────────────────────────────────────────────────────
section("_notify_delivery_slot — nobody eligible at this slot → no Expo call at all")
# ─────────────────────────────────────────────────────────────────────────
sent_batches.clear()
asyncio.run(notif_service._notify_delivery_slot(db_factory, NOW.replace(hour=14)))
check("an empty slot makes zero Expo calls (not an empty-list call either)",
      len(sent_batches) == 0, len(sent_batches))

# ─────────────────────────────────────────────────────────────────────────
section("_notify_streak_alert_slot — real end-to-end, T-65 offset, streak-at-risk user")
# ─────────────────────────────────────────────────────────────────────────
db = SessionLocal()
db.query(DailyBite).delete()
db.add(DailyBite(id="biteA2", user_id="userA", title="t", insight="i", reflection="r", action="a",
                  date=YDAY, library_item_id="bookA", headline="The 1% rule."))
db.add(Streak(id=str(uuid.uuid4()), user_id="userA", current_streak=7, last_active_date=YDAY))
db.query(PushToken).filter(PushToken.user_id == "userA").delete()
db.add(PushToken(id=str(uuid.uuid4()), user_id="userA", token="ExponentPushToken[A]",
                  notification_hour=9, notification_minute=0, streak_alerts_enabled=True))
db.commit()
db.close()

STREAK_NOW = NOW - datetime.timedelta(minutes=65)  # T-65 before the 09:00 delivery slot
sent_batches.clear()
asyncio.run(notif_service._notify_streak_alert_slot(db_factory, STREAK_NOW))
check("exactly one batched call for the streak-alert tick", len(sent_batches) == 1, len(sent_batches))
streak_batch = sent_batches[0] if sent_batches else []
check("the at-risk user's own book is named, with urgency framing",
      len(streak_batch) == 1
      and streak_batch[0]["title"] == "Atomic Habits — streak ends in 1 hour 🔥",
      streak_batch)
check("streak-alert payload deep-links to the exact held bite too",
      len(streak_batch) == 1
      and streak_batch[0]["data"] == {"screen": "Session", "bookId": "bookA", "biteId": "biteA2"},
      streak_batch)

# ─────────────────────────────────────────────────────────────────────────
section("_notify_streak_alert_slot — streak_alerts_enabled=False on the token → no push")
# ─────────────────────────────────────────────────────────────────────────
db = SessionLocal()
tok = db.query(PushToken).filter(PushToken.user_id == "userA").first()
tok.streak_alerts_enabled = False
db.commit()
db.close()

sent_batches.clear()
asyncio.run(notif_service._notify_streak_alert_slot(db_factory, STREAK_NOW))
check("a user who disabled streak alerts on their token gets nothing, even while genuinely at risk",
      len(sent_batches) == 0, len(sent_batches))

print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Task 3 scheduler-integration checks passed")
sys.exit(1 if failures else 0)
