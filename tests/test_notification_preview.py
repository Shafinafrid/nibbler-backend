"""
POST /notifications/send-test — the owner's on-demand notification preview
button, through the real HTTP stack. Rewritten for Task 3 (Aug 2026):
notification copy is now dynamic and book-specific (the bite's own
`headline`, the same LLM-generated "arresting sentence" already shown on
Home/Notebook — reused, never a second AI request), and the payload deep-
links to the exact bite/source instead of a generic Home screen.

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
    sent.append({"tokens": tokens, "title": title, "body": body, "data": data})
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
section("idle account (no unread bite at all) — a clear error, not a fake example")
# ─────────────────────────────────────────────────────────────────────────
sent.clear()
r = client.post("/notifications/send-test")
check("400 on a genuinely idle account (nothing generated yet)", r.status_code == 400, str(r.text))
check("no push was recorded", len(sent) == 0)

# ─────────────────────────────────────────────────────────────────────────
section("unread bite dated TODAY, WITH a real headline — dynamic, book-specific copy")
# ─────────────────────────────────────────────────────────────────────────
db.add(LibraryItem(id="book1", user_id="u1", title="Atomic Habits", type="pdf", processed=True))
db.add(DailyBite(
    id="bite1", user_id="u1", title="t", insight="i", reflection="r", action="a",
    date=TODAY, library_item_id="book1",
    headline="The 1% rule that compounds into everything.",
))
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
check("200", r.status_code == 200, r.text)
body = r.json()
check("title carries the REAL book title, not app-generic copy",
      "Atomic Habits" in body.get("title", ""), body)
check("body is the bite's own real headline (reused, not regenerated)",
      body.get("body") == "The 1% rule that compounds into everything.", body)
check("a real push was actually sent", len(sent) == 1 and sent[0]["tokens"] == ["ExponentPushToken[abc]"])
check("payload deep-links to the exact book AND bite (Task 3 item 7)",
      sent[0]["data"] == {"screen": "Session", "bookId": "book1", "biteId": "bite1"}, sent[0]["data"])

# ─────────────────────────────────────────────────────────────────────────
section("unread bite with NO headline — a varied, book-specific fallback, not one fixed message")
# ─────────────────────────────────────────────────────────────────────────
db.query(DailyBite).delete()
db.add(LibraryItem(id="book2", user_id="u1", title="Deep Work", type="pdf", processed=True))
db.add(DailyBite(
    id="bite2", user_id="u1", title="t", insight="i", reflection="r", action="a",
    date=TODAY, library_item_id="book2", headline=None,
))
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("title still carries the real book title with no headline present",
      "Deep Work" in body.get("title", ""), body)
check("body falls back to a book-specific template (mentions the real title), "
      "not a blank or one permanently fixed string",
      "Deep Work" in body.get("body", "") and body.get("body") != "", body)

from app.services.notification_service import _fallback_hook, _HOOK_FALLBACKS  # noqa: E402
check("more than one fallback template exists (varied, not one fixed message)",
      len(_HOOK_FALLBACKS) > 1)
check("the fallback pick is deterministic per bite (same bite -> same fallback every time)",
      _fallback_hook("Deep Work", "bite2") == _fallback_hook("Deep Work", "bite2"))

# ─────────────────────────────────────────────────────────────────────────
section("unread bite dated YESTERDAY, no active streak — the 'forgotten' framing")
# ─────────────────────────────────────────────────────────────────────────
db.query(DailyBite).delete()
db.add(DailyBite(
    id="bite3", user_id="u1", title="t", insight="i", reflection="r", action="a",
    date=YDAY, library_item_id="book1", headline="A ritual beats a resolution.",
))
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("still book-specific for the forgotten case",
      "Atomic Habits" in body.get("title", ""), body)
check("body reuses the SAME stored headline, not a second AI call, "
      "with reminder framing distinguishing it from the fresh case",
      "A ritual beats a resolution." in body.get("body", "") and body.get("body") != "A ritual beats a resolution.",
      body)

# ─────────────────────────────────────────────────────────────────────────
section("unread bite dated YESTERDAY + a live streak — streak wins, urgency in the title")
# ─────────────────────────────────────────────────────────────────────────
db.add(Streak(id=str(uuid.uuid4()), user_id="u1", current_streak=5, last_active_date=YDAY))
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("streak takes priority over the plain forgotten framing",
      "streak ends in 1 hour" in body.get("title", "").lower(), body)
check("still names the real book in the streak push",
      "Atomic Habits" in body.get("title", ""), body)

# ─────────────────────────────────────────────────────────────────────────
section("streak already saved TODAY — back to forgotten, not streak")
# ─────────────────────────────────────────────────────────────────────────
streak_row = db.query(Streak).filter(Streak.user_id == "u1").first()
streak_row.last_active_date = TODAY
db.commit()
sent.clear()
r = client.post("/notifications/send-test")
body = r.json()
check("a streak already saved today is no longer 'at risk'",
      "streak ends" not in body.get("title", "").lower(), body)

# ─────────────────────────────────────────────────────────────────────────
section("reminder variation: the SAME still-unread bite reads differently across consecutive days")
# ─────────────────────────────────────────────────────────────────────────
from app.services.notification_service import build_notification_copy  # noqa: E402
same_bite = DailyBite(id="bite_var", user_id="u1", title="t", insight="i", reflection="r", action="a",
                       date=YDAY, library_item_id="book1", headline="A ritual beats a resolution.")
seen_bodies = set()
for days_held in range(4):
    _, body_text = build_notification_copy("forgotten", "Atomic Habits", same_bite,
                                            today=YDAY + datetime.timedelta(days=days_held))
    seen_bodies.add(body_text)
check("Task 3 item 12 — reminders about the SAME unread bite vary across "
      "consecutive days rather than repeating one fixed line every time",
      len(seen_bodies) > 1, seen_bodies)
check("but every variant still carries the SAME stored hook — reused, "
      "never a second AI request just to reword the reminder",
      all("A ritual beats a resolution." in b for b in seen_bodies), seen_bodies)
d0 = build_notification_copy("forgotten", "Atomic Habits", same_bite, today=YDAY)[1]
d0_again = build_notification_copy("forgotten", "Atomic Habits", same_bite, today=YDAY)[1]
check("the variation is deterministic — the SAME days-held always produces the SAME framing",
      d0 == d0_again)

# ─────────────────────────────────────────────────────────────────────────
section("no second AI call anywhere on the notification-copy path (Task 3: reuse only)")
# ─────────────────────────────────────────────────────────────────────────
import inspect  # noqa: E402
notif_src = inspect.getsource(notif_service)
check("notification_service.py never imports/constructs an LLM service — "
      "every notification's copy comes from data already generated and "
      "stored during the normal session-generation call, never a fresh one",
      "LLMService" not in notif_src and "generate_wisdom_session" not in notif_src
      and "generate_story_metadata" not in notif_src)

# ─────────────────────────────────────────────────────────────────────────
section("rotation fairness: two unread books (Premium-style) — the featured pick shifts with the date")
# ─────────────────────────────────────────────────────────────────────────
from app.services.notification_service import _feature_bite  # noqa: E402
db.query(DailyBite).delete()
db.add(LibraryItem(id="book3", user_id="u1", title="Sapiens", type="pdf", processed=True))
db.commit()
biteA = DailyBite(id="biteA", user_id="u1", title="t", insight="i", reflection="r", action="a",
                   date=TODAY, library_item_id="book1", headline="A")
biteB = DailyBite(id="biteB", user_id="u1", title="t", insight="i", reflection="r", action="a",
                   date=TODAY, library_item_id="book3", headline="B")
db.add_all([biteA, biteB])
db.commit()

picks = set()
for offset in range(10):
    d = TODAY + datetime.timedelta(days=offset)
    _, title, _ = _feature_bite(db, db.query(User).get("u1"), [biteA, biteB], d)
    picks.add(title)
check("the featured pick among multiple unread bites rotates rather than always picking the same one",
      len(picks) == 2, picks)
check("the pick for a given date is deterministic (stable across repeated calls)",
      _feature_bite(db, db.query(User).get("u1"), [biteA, biteB], TODAY)[1]
      == _feature_bite(db, db.query(User).get("u1"), [biteA, biteB], TODAY)[1])

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all notification-preview checks passed")
sys.exit(1 if failures else 0)
