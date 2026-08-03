"""Verification for Batch A backend fixes (items 20a, 19/41, 10)."""
import os, sys, tempfile, datetime, base64

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/b.db", CLAUDE_API_KEY="t", FIREBASE_PROJECT_ID="t")
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
import main

create_tables()
db = SessionLocal()
db.add(User(id="u1", email="u1@example.com"))
db.add(LibraryItem(id="item1", user_id="u1", title="Book", type="pdf", processed=True))
db.commit()

def _db():
    d = SessionLocal()
    try: yield d
    finally: d.close()

main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = lambda: db.query(User).filter(User.id == "u1").first()
c = TestClient(main.app)

section("SMOKE — all 13 /sync operations still work with the limiter attached")
# A wrong decorator signature breaks the endpoint at call time, not import time,
# so every operation is actually exercised.
calls = [
    ("GET",    "/sync/all",            None),
    ("PUT",    "/sync/notes",          {"book_id": "item1", "card_index": 0, "text": "hello"}),
    ("DELETE", "/sync/notes/none",     None),
    ("PUT",    "/sync/highlights",     {"book_id": "item1", "card_index": 0, "card_title": "First"}),
    ("DELETE", "/sync/highlights/none", None),
    ("POST",   "/sync/chat",           {"book_id": "item1", "role": "user", "content": "hi"}),
    ("DELETE", "/sync/chat/item1",     None),
    ("POST",   "/sync/completions",    {"book_id": "item1", "completed_date": "2026-07-25", "read_length": 5}),
    ("PATCH",  "/sync/settings",       {"read_length": 12}),
    ("PATCH",  "/sync/state",          {"quiz_attempts": 3, "quiz_correct": 2}),
    ("PATCH",  "/sync/identity",       {"timezone": "Europe/Stockholm"}),
    ("PUT",    "/sync/avatar",         {"image_base64": base64.b64encode(b"x" * 64).decode()}),
    ("DELETE", "/sync/avatar",         None),
]
# PUT /sync/avatar uploads to S3. Blank AWS credentials do NOT stop boto3 from
# opening a real TLS connection to AWS and being rejected there — so the old
# "502 is acceptable, no creds in this harness" allowance was quietly passing
# over a genuine outbound request to Amazon on every run. Stub the client
# instead: the endpoint's own logic is what this suite is checking, not S3's.
import app.routers.sync as sync_router  # noqa: E402

_s3_calls = []


class _StubS3:
    def upload_file(self, file_content, filename, content_type=None):
        _s3_calls.append(("upload", filename))
        return filename

    def download_file(self, key):
        _s3_calls.append(("download", key))
        return b"stub-image-bytes"

    def delete_file(self, key):
        _s3_calls.append(("delete", key))
        return True


sync_router.S3Service = _StubS3

for method, path, body in calls:
    r = c.request(method, path, json=body) if body is not None else c.request(method, path)
    ok = r.status_code < 500
    check(f"{method} {path} → {r.status_code}", ok, "" if ok else r.text[:160])

check("avatar upload reached S3 through the stub, not the network",
      ("upload", "u1/avatar.jpg") in _s3_calls, str(_s3_calls))

section("ITEM 20a — re-pushing a highlight refreshes its card text")
c.put("/sync/highlights", json={"book_id": "b9", "card_index": 1, "card_title": "Old title", "card_body": "old"})
c.put("/sync/highlights", json={"book_id": "b9", "card_index": 1, "card_title": "New title", "card_body": "new"})
r = c.get("/sync/all").json()
hl = [h for h in r["highlights"] if h["book_id"] == "b9"]
check("still exactly one row (no duplicate)", len(hl) == 1, f"rows={len(hl)}")
check("card text was updated, not left stale",
      hl and hl[0]["card_title"] == "New title" and hl[0]["card_body"] == "new",
      str(hl[0] if hl else None)[:120])

section("ITEM 19/41 — payload validation")
cases = [
    ("chat role must be user|assistant", "POST", "/sync/chat",
     {"book_id": "b", "role": "system", "content": "x"}, 422),
    ("note text is bounded", "PUT", "/sync/notes",
     {"book_id": "b", "card_index": 0, "text": "x" * 20_001}, 422),
    ("card_index cannot be negative", "PUT", "/sync/notes",
     {"book_id": "b", "card_index": -1, "text": "x"}, 422),
    ("delivery_hour must be a real hour", "PATCH", "/sync/settings",
     {"delivery_hour": 99}, 422),
    ("quiz counters cannot be negative", "PATCH", "/sync/state",
     {"quiz_attempts": -5}, 422),
    ("oversized review_state is refused", "PATCH", "/sync/state",
     {"review_state": {"pad": "x" * 300_000}}, 413),
]
for name, method, path, body, expected in cases:
    r = c.request(method, path, json=body)
    check(name, r.status_code == expected, f"got {r.status_code}, wanted {expected}")

section("ITEM 19/41 — valid payloads still pass (no false rejections)")
ok_cases = [
    ("normal note", "PUT", "/sync/notes", {"book_id": "b", "card_index": 3, "text": "a real note"}),
    ("assistant chat", "POST", "/sync/chat", {"book_id": "b", "role": "assistant", "content": "hi"}),
    ("empty active list is accepted", "PATCH", "/sync/settings", {"active_book_ids": []}),
    ("midnight delivery", "PATCH", "/sync/settings", {"delivery_hour": 0, "delivery_minute": 0}),
    ("realistic review_state", "PATCH", "/sync/state",
     {"review_state": {"day": "2026-07-25", "order": [f"c{i}" for i in range(60)], "pos": 3}}),
]
for name, method, path, body in ok_cases:
    r = c.request(method, path, json=body)
    check(name, r.status_code < 300, f"got {r.status_code}: {r.text[:120]}")

check("an empty settings patch is still a no-op",
      c.request("PATCH", "/sync/settings", json={}).status_code < 300)
check("active_book_ids=[] round-trips as [] (not null)",
      c.get("/sync/all").json()["settings"]["active_book_ids"] == [],
      str(c.get("/sync/all").json()["settings"]["active_book_ids"]))

section("ITEM 10 — generation cap counts server-clock generated_at, not client date")
from app.routers.bites import CAP_WINDOW_HOURS
now = datetime.datetime.utcnow()

def cap_count():
    return db.query(DailyBite).filter(
        DailyBite.user_id == "u1",
        DailyBite.generated_at >= now - datetime.timedelta(hours=CAP_WINDOW_HOURS),
    ).count()

def old_cap_count(client_date):
    return db.query(DailyBite).filter(
        DailyBite.user_id == "u1", DailyBite.date == client_date).count()

today = now.date()
tomorrow = today + datetime.timedelta(days=1)
# One generation a few minutes ago, labelled with TODAY's client date.
db.add(DailyBite(id="gen-now", user_id="u1", library_item_id="item1", title="t",
                 insight="i", reflection="r", action="a", date=today,
                 generated_at=now - datetime.timedelta(minutes=5)))
db.commit()

check("OLD cap: claiming tomorrow's date shows an EMPTY bucket (the exploit)",
      old_cap_count(tomorrow) == 0, f"count={old_cap_count(tomorrow)}")
check("NEW cap: the recent generation is counted regardless of claimed date",
      cap_count() == 1, f"count={cap_count()}")

# A generation from two days ago must not count against today.
db.add(DailyBite(id="gen-old", user_id="u1", library_item_id="item1", title="t",
                 insight="i", reflection="r", action="a",
                 date=today - datetime.timedelta(days=2),
                 generated_at=now - datetime.timedelta(hours=48)))
db.commit()
check("a generation from 48h ago does not count (window is not cumulative)",
      cap_count() == 1, f"count={cap_count()}")
check(f"window is {CAP_WINDOW_HOURS}h, matching the scheduler's lock",
      CAP_WINDOW_HOURS == 23)

# Normal daily use ~24h apart must not be blocked.
db.query(DailyBite).filter(DailyBite.id == "gen-now").update(
    {"generated_at": now - datetime.timedelta(hours=24)})
db.commit()
check("yesterday's nibble at the same time of day has fallen out of the window",
      cap_count() == 0, f"count={cap_count()}")

db.close()
print("\n" + "=" * 62)
print(f"RESULT: {len(failures)} FAILURE(S): {failures}" if failures
      else "RESULT: all Batch A backend checks passed")
sys.exit(1 if failures else 0)
