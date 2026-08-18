"""
POST /notifications/send-test — the owner's on-demand notification preview
button, through the real HTTP stack.

The whole point of this endpoint is that it reuses the REAL scheduler's
title/body selection (`preview_notification_for_user`) rather than a
client-side mockup, so these tests exercise that shared function with real
DailyBite/Streak rows rather than mocking it away.

    .venv/bin/python tests/test_notification_preview.py
"""

import datetime
import os
import sys
import tempfile
import uuid

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/notif.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from fastapi.testclient import TestClient  # noqa: E402

from app.database import create_tables, SessionLocal, get_db  # noqa: E402
from app.middleware.auth import get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.push_token import PushToken  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
from app.models.streak import Streak  # noqa: E402
import app.routers.notifications as notif_router  # noqa: E402
import main  # noqa: E402

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

create_tables()
db = SessionLocal()

# ── a stub that records sends instead of dialing Expo ───────────────────────
sent = []
async def _fake_send(tokens, title, body, data=None, expo_access_token=""):
    sent.append({"tokens": tokens, "title": title, "body": body})
    return [{"status": "ok"} for _ in tokens]
notif_router.send_push_notifications = _fake_send
import app.services.notification_service as notif_service
notif_service.send_push_notifications = _fake_send

db.add(User(id="u1", email="u1@example.com"))
db.commit()

AS = {"id": "u1"}
main.app.dependency_overrides[get_db] = lambda: db
main.app.dependency_overrides[get_current_user] = \
    lambda: db.query(User).filter(User.id == AS["id"]).first()
client = TestClient(main.app)

TODAY = datetime.date.today()
YDAY = TODAY - datetime.timedelta(days=1)

# ─────────────────────────────────────────────────────────────────────────
section("no push token registered — a clear 400, not a silent no-op")
# ─────────────────────────────────────────────────────────────────────────
r = client.post("/notifications/send-test")
check("returns 400 when no token is registered", r.status_code == 400, str(r.status_code))
check("no push was recorded", len(sent) == 0)

# Register a token for the rest of the tests.
db.add(PushToken(id=str(uuid.uuid4()), user_id="u1", token="ExponentPushToken[abc]"))
db.commit()

# ─────────────────────────────────────────────────────────────────────────
section("idle account (no unread bite, no streak) — falls back to the everyday copy")
# ─────────────────────────────────────────────────────────────────────────
sent.clear()
r = client.post("/notifications/send-test")
check("200 on an idle account", r.status_code == 200, str(r.text))
body = r.json()
check("falls back to the FRESH variant", body.get("title") == "Your daily nibble is ready 🐱", body)
check("a real push was actually sent", len(sent) == 1 and sent[0]["tokens"] == ["ExponentPushToken[abc]"])

# ─────────────────────────────────────────────────────────────────────────
section("unread bite dated TODAY — the fresh-nibble variant")
# ─────────────────────────────────────────────────────────────────────────
db.add(LibraryItem(id="book1", user_id="u1", title="Book One", type="pdf", processed=True))
db.add(DailyBite(
    id=str(uuid.uuid4()), user_id="u1", title="t", insight="i", reflection="r", action="a",
    date=TODAY, library_item_id="book1",
))
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("a today-dated unread bite produces the FRESH variant",
      body.get("title") == "Your daily nibble is ready 🐱", body)

# ─────────────────────────────────────────────────────────────────────────
section("unread bite dated YESTERDAY, no active streak — the forgotten-nibble variant")
# ─────────────────────────────────────────────────────────────────────────
db.query(DailyBite).delete()
db.add(DailyBite(
    id=str(uuid.uuid4()), user_id="u1", title="t", insight="i", reflection="r", action="a",
    date=YDAY, library_item_id="book1",
))
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("a yesterday-dated unread bite with no streak produces the FORGOTTEN variant",
      body.get("title") == "Psst… you forgot yesterday's nibble 🐱", body)

# ─────────────────────────────────────────────────────────────────────────
section("unread bite dated YESTERDAY + a live streak — streak wins over forgotten")
# ─────────────────────────────────────────────────────────────────────────
db.add(Streak(id=str(uuid.uuid4()), user_id="u1", current_streak=5, last_active_date=YDAY))
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("an at-risk streak takes priority over the plain forgotten variant "
      "(otherwise the streak copy would be unreachable through this button "
      "whenever it's actually true, since streak_at_risk implies forgotten)",
      body.get("title") == "Your streak ends in 1 hour 🔥", body)

# ─────────────────────────────────────────────────────────────────────────
section("streak already saved TODAY — back to forgotten, not streak")
# ─────────────────────────────────────────────────────────────────────────
streak_row = db.query(Streak).filter(Streak.user_id == "u1").first()
streak_row.last_active_date = TODAY
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("a streak already saved today is no longer 'at risk', falls back to FORGOTTEN",
      body.get("title") == "Psst… you forgot yesterday's nibble 🐱", body)

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all notification-preview checks passed")
sys.exit(1 if failures else 0)
