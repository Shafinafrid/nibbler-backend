"""
Task 16 — Preserve honest failed-chat state and retry without duplicating
questions. Backend half: the ChatTurn idempotency primitive that makes
POST /connect/chat safe to retry without risking a second paid LLM call
for a question that may have already succeeded.

Investigation confirmed both of the audit's core claims were true: (1) the
user's message persisted durably before the request even completed, while
the failure explanation existed only in transient React state — a client-
side concern, fixed in ConnectChatScreen.js/sessionStore.js, not tested
here; (2) retrying re-sent the SAME text as a brand-new request with zero
memory of the prior attempt — meaning a slow-but-still-succeeding first
call and a second "just in case" call could both complete and both get
billed. This file proves the backend half of the fix: a client-generated
turn id, reused unchanged on retry, that this endpoint uses to recognise
"already answered" (replay, no LLM call), "already in progress" (refuse,
no LLM call), or "genuinely needs a fresh attempt" (crash-recovered lease,
or an explicit retry of a real failure) — never two concurrent or
duplicate generations for one logical question.

  A — a fresh turn: creates the ChatTurn row, calls the LLM exactly once,
      completes it.
  B — retrying the SAME turn id after success is a pure cache replay — the
      exact scenario that matters most: ZERO additional LLM calls.
  C — a turn still genuinely in progress (lease not expired) refuses a
      concurrent/duplicate request outright, no second LLM call.
  D — retrying after a definitive failure re-opens the turn for exactly
      one more attempt — succeeds this time, LLM called once for the
      retry (not twice for the whole story).
  E — a crashed worker's expired lease is safely reclaimed by the next
      request — never stuck 'pending' forever.
  F — cross-user isolation: the SAME turn id from two different accounts
      never leaks one user's data to the other.
  G — no client turn id (an old, not-yet-updated app build) still works
      exactly as before — just without the extra protection.
  H — the same turn id reused for a DIFFERENT book is refused as a
      conflict, not silently misattributed.
  I — a generation failure never leaks the raw exception to the client.
  J — a validation-type rejection (no indexed content) marks the turn
      'failed' WITHOUT ever calling the LLM — never billed.
  K — premium/entitlement rejection happens before any ChatTurn row is
      even created — no orphaned turns for requests that were never
      going to be answered.
"""
import os
import sys
import tempfile
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task16.db", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports

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
from app.models.user_data import ChatTurn
import main
import datetime as _dt

create_tables()
db = SessionLocal()

# Well outside the 7-day signup trial (effective_premium's other grant
# path) — every test user needs is_premium to be the ONLY thing deciding
# their entitlement, or section K's "non-premium is refused" case is
# silently granted premium anyway by a freshly-created user's own trial.
OLD = _dt.datetime.utcnow() - _dt.timedelta(days=400)

CURRENT_UID = {"v": None}
def _db(): yield db
def _current_user(): return db.query(User).filter(User.id == CURRENT_UID["v"]).first()
main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = _current_user
c = TestClient(main.app)
def as_user(uid): CURRENT_UID["v"] = uid


def mkuser(uid, **kw):
    kw.setdefault("email", f"{uid}@example.com")
    kw.setdefault("is_premium", True)
    kw.setdefault("created_at", OLD)
    u = User(id=uid, **kw)
    db.add(u); db.commit()
    return u


def mkitem(iid, uid, **kw):
    kw.setdefault("type", "pdf")
    kw.setdefault("title", iid)
    kw.setdefault("processed", True)
    kw.setdefault("content", "Some real book content that stands in for indexed text.")
    it = LibraryItem(id=iid, user_id=uid, **kw)
    db.add(it); db.commit()
    return it


def refresh_turn(turn_id, uid):
    db.expire_all()
    return db.query(ChatTurn).filter(ChatTurn.turn_id == turn_id, ChatTurn.user_id == uid).first()


def chat_payload(item_id, message, turn_id=None, history=None):
    p = {"library_item_id": item_id, "message": message, "history": history or []}
    if turn_id is not None:
        p["turn_id"] = turn_id
    return p


# ═══════════════════════════════════════════════════════════════════════
section("A — a fresh turn calls the LLM exactly once and completes")
# ═══════════════════════════════════════════════════════════════════════
u_a = mkuser("t16_a")
mkitem("t16_item_a", "t16_a")
as_user("t16_a")

with mock.patch("app.routers.connect.LLMService") as MockLLM, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed:
    MockEmbed.return_value.search_item.return_value = ["an excerpt from the book"]
    MockLLM.return_value.chat_with_book.return_value = "Here's what the book says."
    r = c.post("/connect/chat", json=chat_payload("t16_item_a", "What is this book about?", turn_id="turn_a1"))

check("first request succeeds", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
check("reply matches the mocked LLM output", r.json().get("reply") == "Here's what the book says.")
check("the LLM was called exactly once", MockLLM.return_value.chat_with_book.call_count == 1)

turn_a = refresh_turn("turn_a1", "t16_a")
check("a ChatTurn row exists, status 'completed'", turn_a is not None and turn_a.status == "completed")
check("the canonical reply is stored on the row", turn_a.reply == "Here's what the book says.")


# ═══════════════════════════════════════════════════════════════════════
section("B — retrying the SAME turn id after success is a pure cache replay (zero new LLM calls)")
# ═══════════════════════════════════════════════════════════════════════
with mock.patch("app.routers.connect.LLMService") as MockLLM2, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed2:
    MockEmbed2.return_value.search_item.return_value = ["should never be reached"]
    MockLLM2.return_value.chat_with_book.return_value = "THIS WOULD BE A SECOND BILLED CALL"
    r = c.post("/connect/chat", json=chat_payload("t16_item_a", "What is this book about?", turn_id="turn_a1"))

check("the retry still returns 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
check("the retry returns the ORIGINAL cached reply, not a new one",
      r.json().get("reply") == "Here's what the book says.", r.json())
check("THE LLM WAS NEVER CALLED FOR THE RETRY — the exact money-safety property this task exists for",
      MockLLM2.return_value.chat_with_book.call_count == 0)
check("embeddings were never even searched for the replay", MockEmbed2.return_value.search_item.call_count == 0)


# ═══════════════════════════════════════════════════════════════════════
section("C — a turn genuinely still in progress refuses a concurrent/duplicate request outright")
# ═══════════════════════════════════════════════════════════════════════
u_c = mkuser("t16_c")
mkitem("t16_item_c", "t16_c")
as_user("t16_c")

# Simulate "a request is already generating right now" by directly writing a
# 'pending' row with a lease that has NOT expired — exactly what the first
# half of a real in-flight request would look like mid-generation.
import datetime as _dt
db.add(ChatTurn(turn_id="turn_c1", user_id="t16_c", book_id="t16_item_c", status="pending",
                question="in flight", claimed_by="some-other-worker",
                claimed_until=_dt.datetime.utcnow() + _dt.timedelta(minutes=2)))
db.commit()

with mock.patch("app.routers.connect.LLMService") as MockLLM3, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed3:
    r = c.post("/connect/chat", json=chat_payload("t16_item_c", "in flight", turn_id="turn_c1"))

check("a genuinely in-progress turn is refused (409 chat_turn_processing)",
      r.status_code == 409 and r.json().get("detail", {}).get("code") == "chat_turn_processing",
      f"{r.status_code} {r.text[:150]}")
check("no LLM call was made for the refused duplicate", MockLLM3.return_value.chat_with_book.call_count == 0)


# ═══════════════════════════════════════════════════════════════════════
section("D — retrying after a DEFINITIVE FAILURE re-opens the turn for exactly one more attempt")
# ═══════════════════════════════════════════════════════════════════════
u_d = mkuser("t16_d")
mkitem("t16_item_d", "t16_d")
as_user("t16_d")

with mock.patch("app.routers.connect.LLMService") as MockLLM4, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed4:
    MockEmbed4.return_value.search_item.return_value = ["excerpt"]
    MockLLM4.return_value.chat_with_book.side_effect = RuntimeError("provider exploded")
    r1 = c.post("/connect/chat", json=chat_payload("t16_item_d", "will this work?", turn_id="turn_d1"))

check("the first attempt fails with a safe generic message",
      r1.status_code == 502 and r1.json().get("detail", {}).get("code") == "generation_failed",
      f"{r1.status_code} {r1.text[:150]}")
turn_d_after_fail = refresh_turn("turn_d1", "t16_d")
check("the turn is durably marked 'failed' after the definitive failure",
      turn_d_after_fail.status == "failed", turn_d_after_fail.status if turn_d_after_fail else None)

with mock.patch("app.routers.connect.LLMService") as MockLLM5, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed5:
    MockEmbed5.return_value.search_item.return_value = ["excerpt"]
    MockLLM5.return_value.chat_with_book.return_value = "Yes, it works now."
    r2 = c.post("/connect/chat", json=chat_payload("t16_item_d", "will this work?", turn_id="turn_d1"))

check("the retry (SAME turn id) succeeds", r2.status_code == 200 and r2.json().get("reply") == "Yes, it works now.",
      f"{r2.status_code} {r2.text[:150]}")
check("the retry calls the LLM exactly once (one attempt, not a pile-up)",
      MockLLM5.return_value.chat_with_book.call_count == 1)
check("the turn is now 'completed', with the RETRY's reply, not the failure",
      refresh_turn("turn_d1", "t16_d").status == "completed")


# ═══════════════════════════════════════════════════════════════════════
section("E — a crashed worker's expired lease is safely reclaimed, never stuck forever")
# ═══════════════════════════════════════════════════════════════════════
u_e = mkuser("t16_e")
mkitem("t16_item_e", "t16_e")
as_user("t16_e")

# A 'pending' row whose lease already expired — simulates a worker that
# claimed the turn and then died before ever finishing (crash, OOM kill,
# deploy restart) — never released, never completed.
db.add(ChatTurn(turn_id="turn_e1", user_id="t16_e", book_id="t16_item_e", status="pending",
                question="are you still there?", claimed_by="dead-worker",
                claimed_until=_dt.datetime.utcnow() - _dt.timedelta(minutes=1)))
db.commit()

with mock.patch("app.routers.connect.LLMService") as MockLLM6, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed6:
    MockEmbed6.return_value.search_item.return_value = ["excerpt"]
    MockLLM6.return_value.chat_with_book.return_value = "Yes, still here."
    r = c.post("/connect/chat", json=chat_payload("t16_item_e", "are you still there?", turn_id="turn_e1"))

check("a request against a stale (dead-worker) lease is NOT refused as 'in progress' — "
      "it's reclaimed and actually answered", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
check("the LLM was called for the reclaim (this is a genuine recovery, not a replay)",
      MockLLM6.return_value.chat_with_book.call_count == 1)
check("the turn is now 'completed'", refresh_turn("turn_e1", "t16_e").status == "completed")


# ═══════════════════════════════════════════════════════════════════════
section("F — cross-user isolation: the SAME turn id from two different accounts never leaks")
# ═══════════════════════════════════════════════════════════════════════
u_f1 = mkuser("t16_f1")
mkitem("t16_item_f1", "t16_f1")
u_f2 = mkuser("t16_f2")
mkitem("t16_item_f2", "t16_f2")

as_user("t16_f1")
with mock.patch("app.routers.connect.LLMService") as MockLLMf1, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedf1:
    MockEmbedf1.return_value.search_item.return_value = ["excerpt"]
    MockLLMf1.return_value.chat_with_book.return_value = "Answer for user F1, private."
    c.post("/connect/chat", json=chat_payload("t16_item_f1", "q", turn_id="shared_turn_id"))

as_user("t16_f2")
with mock.patch("app.routers.connect.LLMService") as MockLLMf2, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedf2:
    MockEmbedf2.return_value.search_item.return_value = ["excerpt"]
    MockLLMf2.return_value.chat_with_book.return_value = "Answer for user F2, unrelated."
    r_f2 = c.post("/connect/chat", json=chat_payload("t16_item_f2", "q", turn_id="shared_turn_id"))

check("user F2 gets their OWN answer, not F1's cached reply, despite the identical turn id",
      r_f2.status_code == 200 and r_f2.json().get("reply") == "Answer for user F2, unrelated.",
      f"{r_f2.status_code} {r_f2.text[:150]}")
check("the LLM WAS called for F2 — no cross-user replay happened", MockLLMf2.return_value.chat_with_book.call_count == 1)
check("two independent ChatTurn rows exist, correctly scoped per user",
      refresh_turn("shared_turn_id", "t16_f1") is not None and refresh_turn("shared_turn_id", "t16_f2") is not None)
check("F1's row still has F1's own reply, untouched by F2's request",
      refresh_turn("shared_turn_id", "t16_f1").reply == "Answer for user F1, private.")


# ═══════════════════════════════════════════════════════════════════════
section("G — no client turn id (old app build) still works exactly as before")
# ═══════════════════════════════════════════════════════════════════════
u_g = mkuser("t16_g")
mkitem("t16_item_g", "t16_g")
as_user("t16_g")

with mock.patch("app.routers.connect.LLMService") as MockLLMg, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedg:
    MockEmbedg.return_value.search_item.return_value = ["excerpt"]
    MockLLMg.return_value.chat_with_book.return_value = "Works without a turn id too."
    r = c.post("/connect/chat", json=chat_payload("t16_item_g", "hello", turn_id=None))

check("a request with NO turn_id field at all still succeeds (backward compatible)",
      r.status_code == 200 and r.json().get("reply") == "Works without a turn id too.",
      f"{r.status_code} {r.text[:150]}")


# ═══════════════════════════════════════════════════════════════════════
section("H — the same turn id reused for a DIFFERENT book is refused as a conflict")
# ═══════════════════════════════════════════════════════════════════════
u_h = mkuser("t16_h")
mkitem("t16_item_h1", "t16_h")
mkitem("t16_item_h2", "t16_h")
as_user("t16_h")

with mock.patch("app.routers.connect.LLMService") as MockLLMh, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedh:
    MockEmbedh.return_value.search_item.return_value = ["excerpt"]
    MockLLMh.return_value.chat_with_book.return_value = "book 1 answer"
    c.post("/connect/chat", json=chat_payload("t16_item_h1", "q", turn_id="turn_h1"))

with mock.patch("app.routers.connect.LLMService") as MockLLMh2, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedh2:
    r = c.post("/connect/chat", json=chat_payload("t16_item_h2", "q", turn_id="turn_h1"))

check("reusing a turn id against a DIFFERENT book_id is refused (409 chat_turn_conflict), "
      "not silently misattributed to the wrong book",
      r.status_code == 409 and r.json().get("detail", {}).get("code") == "chat_turn_conflict",
      f"{r.status_code} {r.text[:150]}")
check("no LLM call was made for the conflicting request", MockLLMh2.return_value.chat_with_book.call_count == 0)


# ═══════════════════════════════════════════════════════════════════════
section("I — a generation failure never leaks the raw exception to the client")
# ═══════════════════════════════════════════════════════════════════════
u_i = mkuser("t16_i")
mkitem("t16_item_i", "t16_i")
as_user("t16_i")

with mock.patch("app.routers.connect.LLMService") as MockLLMi, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedi:
    MockEmbedi.return_value.search_item.return_value = ["excerpt"]
    MockLLMi.return_value.chat_with_book.side_effect = RuntimeError(
        "SENSITIVE_PROVIDER_INTERNAL_DETAIL sk-secret-key-1234")
    r = c.post("/connect/chat", json=chat_payload("t16_item_i", "q", turn_id="turn_i1"))

check("the raw exception text never appears anywhere in the response body",
      "SENSITIVE_PROVIDER_INTERNAL_DETAIL" not in r.text and "sk-secret-key-1234" not in r.text,
      r.text[:200])
check("a fixed, safe error message is returned instead",
      r.json().get("detail", {}).get("message") == "Something went wrong generating a reply — you can try again.",
      r.text[:200])


# ═══════════════════════════════════════════════════════════════════════
section("J — a validation-type rejection (no indexed content) never calls the LLM, still marks the turn failed")
# ═══════════════════════════════════════════════════════════════════════
u_j = mkuser("t16_j")
mkitem("t16_item_j", "t16_j", content=None)   # no content, no chunks — nothing to answer from
as_user("t16_j")

with mock.patch("app.routers.connect.LLMService") as MockLLMj, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedj:
    MockEmbedj.return_value.search_item.return_value = []   # nothing indexed
    r = c.post("/connect/chat", json=chat_payload("t16_item_j", "q", turn_id="turn_j1"))

check("a no-content book is refused (422 no_content)",
      r.status_code == 422 and r.json().get("detail", {}).get("code") == "no_content",
      f"{r.status_code} {r.text[:150]}")
check("the LLM is NEVER called for a request that was always going to be refused",
      MockLLMj.return_value.chat_with_book.call_count == 0)
check("the turn is still durably marked 'failed' (so a client showing a Retry button has "
      "something honest to point at)", refresh_turn("turn_j1", "t16_j").status == "failed")


# ═══════════════════════════════════════════════════════════════════════
section("K — premium rejection happens before any ChatTurn row is created (no orphaned turns)")
# ═══════════════════════════════════════════════════════════════════════
u_k = mkuser("t16_k", is_premium=False)
mkitem("t16_item_k", "t16_k")
as_user("t16_k")

r = c.post("/connect/chat", json=chat_payload("t16_item_k", "q", turn_id="turn_k1"))
check("a non-premium user is refused (403 premium_required)",
      r.status_code == 403 and r.json().get("detail", {}).get("code") == "premium_required",
      f"{r.status_code} {r.text[:150]}")
check("no ChatTurn row was ever created for the refused request",
      refresh_turn("turn_k1", "t16_k") is None)


print("\n" + "=" * 62)
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: all Task 16 backend chat-safety checks passed")
sys.exit(0)
