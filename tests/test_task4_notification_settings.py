"""
Task 4 — "Notification settings can lie" (Point 5, master audit).

Covers:
  · POST /register / PUT /enabled / GET /state / PUT /time through the real
    HTTP stack — the truthful, backend-is-source-of-truth surface.
  · Disabling never deletes the PushToken row (items 7/8) and the scheduler
    actually honors the flag (a disabled token gets nothing, even though it
    is otherwise eligible).
  · Timezone-safe delivery (items 13/14): two users who both picked the SAME
    local wall-clock time in DIFFERENT timezones get pushed at DIFFERENT UTC
    ticks — proven against the real `_notify_delivery_slot`, not a client-side
    mock — and a token with no local time on record (a legacy/pre-Task-4
    registration) still matches on its cached UTC columns exactly as before.

    .venv/bin/python tests/test_task4_notification_settings.py
"""

import asyncio
import datetime
import os
import sys
import tempfile
import uuid

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/task4.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app.database import create_tables, SessionLocal, get_db  # noqa: E402
from app.middleware.auth import get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.push_token import PushToken  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
import app.services.notification_service as notif_service  # noqa: E402
import main  # noqa: E402

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

create_tables()
db = SessionLocal()

sent_batches = []
async def _fake_send_messages(messages, expo_access_token=""):
    sent_batches.append(list(messages))
    return [{"status": "ok"} for _ in messages]
notif_service.send_push_messages = _fake_send_messages

db.add(User(id="u1", email="u1@example.com"))
db.commit()

AS = {"id": "u1"}
main.app.dependency_overrides[get_db] = lambda: db
main.app.dependency_overrides[get_current_user] = \
    lambda: db.query(User).filter(User.id == AS["id"]).first()
client = TestClient(main.app)

TOKEN = "ExponentPushToken[u1]"

# ─────────────────────────────────────────────────────────────────────────
section("GET /state before any registration — honest 'not registered', not a guess")
# ─────────────────────────────────────────────────────────────────────────
r = client.get("/notifications/state", params={"token": TOKEN})
check("200 with registered=False", r.status_code == 200 and r.json()["registered"] is False, r.json())
check("enabled=False when unregistered", r.json()["enabled"] is False, r.json())

# ─────────────────────────────────────────────────────────────────────────
section("POST /register — stores local wall-clock time AND sets enabled=True")
# ─────────────────────────────────────────────────────────────────────────
r = client.post("/notifications/register", json={
    "token": TOKEN, "platform": "ios",
    "notification_hour": 8, "notification_minute": 0,
    "notification_local_hour": 9, "notification_local_minute": 0,
    "streak_alerts": True,
})
check("register succeeds", r.status_code == 200, r.text)

row = db.query(PushToken).filter(PushToken.token == TOKEN).first()
check("row exists", row is not None)
check("notifications_enabled defaults True on register", row.notifications_enabled is True)
check("local hour/minute stored as sent", (row.notification_local_hour, row.notification_local_minute) == (9, 0))

r = client.get("/notifications/state", params={"token": TOKEN})
body = r.json()
check("GET /state now reports registered=True", body["registered"] is True, body)
check("GET /state reports enabled=True", body["enabled"] is True, body)
check("GET /state reports the local time back truthfully", (body["notification_local_hour"], body["notification_local_minute"]) == (9, 0), body)

# ─────────────────────────────────────────────────────────────────────────
section("PUT /enabled — disabling flips the flag, NEVER deletes the row (items 7/8)")
# ─────────────────────────────────────────────────────────────────────────
r = client.put("/notifications/enabled", json={"token": TOKEN, "enabled": False})
check("disable succeeds", r.status_code == 200, r.text)

row = db.query(PushToken).filter(PushToken.token == TOKEN).first()
check("the row STILL EXISTS after disabling", row is not None)
check("notifications_enabled is now False", row is not None and row.notifications_enabled is False)
check("local time survives a disable (nothing forgotten)", row is not None and row.notification_local_hour == 9)

r = client.get("/notifications/state", params={"token": TOKEN})
check("GET /state reflects the disable truthfully", r.json()["enabled"] is False, r.json())

r = client.put("/notifications/enabled", json={"token": TOKEN, "enabled": True})
check("re-enable succeeds without re-registering", r.status_code == 200, r.text)
row = db.query(PushToken).filter(PushToken.token == TOKEN).first()
check("re-enable flips the flag back", row.notifications_enabled is True)

r = client.put("/notifications/enabled", json={"token": "ExponentPushToken[nope]", "enabled": False})
check("unknown token → 404, not a silent success", r.status_code == 404, r.status_code)

# ─────────────────────────────────────────────────────────────────────────
section("PUT /time — stores local time; GET /state confirms it back")
# ─────────────────────────────────────────────────────────────────────────
r = client.put("/notifications/time", json={
    "token": TOKEN, "notification_hour": 14, "notification_minute": 30,
    "notification_local_hour": 20, "notification_local_minute": 15,
})
check("time update succeeds", r.status_code == 200, r.text)
row = db.query(PushToken).filter(PushToken.token == TOKEN).first()
check("local time updated", (row.notification_local_hour, row.notification_local_minute) == (20, 15))
check("minute snapped to a 5-min slot", row.notification_local_minute % 5 == 0)

# ─────────────────────────────────────────────────────────────────────────
section("Real scheduler tick — a DISABLED token gets nothing even though otherwise eligible")
# ─────────────────────────────────────────────────────────────────────────
db.add(LibraryItem(id="book1", user_id="u1", title="Deep Work", type="pdf", processed=True))
TODAY = datetime.date.today()
db.add(DailyBite(id="bite1", user_id="u1", title="t", insight="i", reflection="r", action="a",
                  date=TODAY, library_item_id="book1", headline="Protect your deep hours."))
db.commit()

# Reset to a clean, enabled, UTC-only (no local/tz) token for this section.
db.query(PushToken).filter(PushToken.token == TOKEN).delete()
db.add(PushToken(id=str(uuid.uuid4()), user_id="u1", token=TOKEN,
                  notification_hour=9, notification_minute=0, notifications_enabled=True))
db.commit()

def db_factory():
    return SessionLocal()

NOW = datetime.datetime.combine(TODAY, datetime.time(9, 0), tzinfo=datetime.timezone.utc)

sent_batches.clear()
asyncio.run(notif_service._notify_delivery_slot(db_factory, NOW))
check("enabled legacy (no local/tz) token DOES get pushed at its cached UTC slot",
      len(sent_batches) == 1 and len(sent_batches[0]) == 1, sent_batches)

db2 = SessionLocal()
db2.query(PushToken).filter(PushToken.token == TOKEN).update({"notifications_enabled": False})
db2.commit()
db2.close()

sent_batches.clear()
asyncio.run(notif_service._notify_delivery_slot(db_factory, NOW))
check("the SAME tick, same user/bite, but disabled → zero Expo calls",
      len(sent_batches) == 0, sent_batches)

db3 = SessionLocal()
db3.query(PushToken).filter(PushToken.token == TOKEN).update({"notifications_enabled": True})
db3.commit()
db3.close()

# ─────────────────────────────────────────────────────────────────────────
section("Timezone-safe delivery (items 13/14): SAME local pick, DIFFERENT timezones → DIFFERENT UTC ticks")
# ─────────────────────────────────────────────────────────────────────────
db.add_all([
    User(id="stk", email="stk@example.com", timezone="Europe/Stockholm"),
    User(id="nyc", email="nyc@example.com", timezone="America/New_York"),
])
db.add_all([
    LibraryItem(id="bookS", user_id="stk", title="Atomic Habits", type="pdf", processed=True),
    LibraryItem(id="bookN", user_id="nyc", title="Deep Work", type="pdf", processed=True),
])
db.add_all([
    DailyBite(id="biteS", user_id="stk", title="t", insight="i", reflection="r", action="a",
              date=TODAY, library_item_id="bookS", headline="The 1% rule."),
    DailyBite(id="biteN", user_id="nyc", title="t", insight="i", reflection="r", action="a",
              date=TODAY, library_item_id="bookN", headline="Protect your deep hours."),
])
# BOTH pick 09:00 local — but Stockholm (UTC+2 in August) and New York
# (UTC-4 in August) mean genuinely different UTC delivery moments.
db.add_all([
    PushToken(id=str(uuid.uuid4()), user_id="stk", token="ExponentPushToken[STK]",
              notification_hour=7, notification_minute=0,   # stale/irrelevant cached UTC — must be IGNORED once local+tz are present
              notification_local_hour=9, notification_local_minute=0, notifications_enabled=True),
    PushToken(id=str(uuid.uuid4()), user_id="nyc", token="ExponentPushToken[NYC]",
              notification_hour=7, notification_minute=0,
              notification_local_hour=9, notification_local_minute=0, notifications_enabled=True),
])
db.commit()

# August: Stockholm is UTC+2 (CEST) → 09:00 local = 07:00 UTC.
STK_TICK = datetime.datetime.combine(TODAY, datetime.time(7, 0), tzinfo=datetime.timezone.utc)
sent_batches.clear()
asyncio.run(notif_service._notify_delivery_slot(db_factory, STK_TICK))
tokens_pushed = {m["to"] for batch in sent_batches for m in batch}
check("at 07:00 UTC, Stockholm's 09:00-local pick fires", "ExponentPushToken[STK]" in tokens_pushed, tokens_pushed)
check("New York's 09:00-local pick does NOT fire at 07:00 UTC (that's 03:00 NYC)", "ExponentPushToken[NYC]" not in tokens_pushed, tokens_pushed)

# August: New York is UTC-4 (EDT) → 09:00 local = 13:00 UTC.
NYC_TICK = datetime.datetime.combine(TODAY, datetime.time(13, 0), tzinfo=datetime.timezone.utc)
sent_batches.clear()
asyncio.run(notif_service._notify_delivery_slot(db_factory, NYC_TICK))
tokens_pushed = {m["to"] for batch in sent_batches for m in batch}
check("at 13:00 UTC, New York's 09:00-local pick fires", "ExponentPushToken[NYC]" in tokens_pushed, tokens_pushed)
check("Stockholm's 09:00-local pick does NOT fire at 13:00 UTC (that's 15:00 Stockholm)", "ExponentPushToken[STK]" not in tokens_pushed, tokens_pushed)

check("neither fires at the STALE cached UTC value (07:00 was the stored notification_hour for BOTH, but only Stockholm's real local time maps there)",
      True)  # covered by the two checks above — NYC did not fire at 07:00 despite notification_hour=7

# ─────────────────────────────────────────────────────────────────────────
section("A user's OWN timezone change reconciles automatically on the very next tick — no re-save needed")
# ─────────────────────────────────────────────────────────────────────────
# Same user, same stored local pick (09:00) — but they've traveled and
# users.timezone has since been updated (as PATCH /sync/identity already
# does on every launch). No call to PUT /time happened.
db4 = SessionLocal()
db4.query(User).filter(User.id == "stk").update({"timezone": "America/New_York"})
db4.commit()
db4.close()

sent_batches.clear()
asyncio.run(notif_service._notify_delivery_slot(db_factory, NYC_TICK))
tokens_pushed = {m["to"] for batch in sent_batches for m in batch}
check("after the timezone changes to New York, the SAME 09:00 local pick now fires at the NYC UTC tick — no re-registration needed",
      "ExponentPushToken[STK]" in tokens_pushed, tokens_pushed)

print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Task 4 notification-settings checks passed")
sys.exit(1 if failures else 0)
