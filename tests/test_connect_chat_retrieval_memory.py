"""
Connect chat retrieval-memory fix (Aug 2026).

Reproduces the exact failure a live user hit: chatting about "The Intelligent
Investor," asking about dollar-cost averaging got a detailed, accurate
answer citing Chapter 5 — then a follow-up ("is that all the book says
about this?") made the model FLATLY DENY that content existed, because
POST /connect/chat used to run one fresh vector search per turn, querying
with ONLY that turn's raw message text. A differently-worded follow-up
embeds to a different top-8, so the model genuinely stopped seeing the
chunk it had correctly cited one turn earlier.

The fix (app/routers/connect.py `_gather_chat_excerpts`,
app/services/connect_retrieval.py, ChatContextChunk in
app/models/user_data.py):

  1. Every chunk_index a (user, book) conversation has EVER surfaced is
     persisted and UNIONED into every later turn's context — a chunk shown
     once stays visible for the rest of the conversation.
  2. A vague follow-up ("is that all?", "tell me more") gets the PREVIOUS
     user question substituted in as the retrieval query, instead of being
     embedded literally (connect_retrieval.expand_query).
  3. A broad "everything about X" question widens top_k and searches with
     both the expanded and raw query (connect_retrieval.is_broad_coverage_question).

Sections:
  A — a chunk surfaced in turn 1 is still in the model's context in turn 3,
      even though turn 2's OWN fresh search returns something unrelated —
      this is the direct repro of the live bug.
  B — a vague follow-up's retrieval query is the PRIOR question's text, not
      the vague text itself.
  C — a broad-coverage question searches with a wider top_k than a normal one.
  D — accumulated context is scoped per (user, book) — a second book for the
      same user starts with no inherited chunks.
  E — a chunk found ONLY by the keyword fallback net (no vector-search hit)
      still gets its text resolved and reaches the model, and is persisted.
"""
import os
import sys
import tempfile
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/connect_retrieval_memory.db", FIREBASE_PROJECT_ID="t")
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
from app.models.user_data import ChatContextChunk
import main
import datetime as _dt

create_tables()
db = SessionLocal()

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


def chat_payload(item_id, message, turn_id=None, history=None):
    p = {"library_item_id": item_id, "message": message, "history": history or []}
    if turn_id is not None:
        p["turn_id"] = turn_id
    return p


def persisted_indexes(uid, book_id):
    db.expire_all()
    rows = (
        db.query(ChatContextChunk.chunk_index)
        .filter(ChatContextChunk.user_id == uid, ChatContextChunk.book_id == book_id)
        .all()
    )
    return {r[0] for r in rows}


# ═══════════════════════════════════════════════════════════════════════
section("A — a chunk surfaced in turn 1 is still visible to the model in turn 3, "
        "even though turn 2/3's OWN fresh search finds something else — the live repro")
# ═══════════════════════════════════════════════════════════════════════
u_a = mkuser("crm_a")
mkitem("crm_item_a", "crm_a")
as_user("crm_a")

DCA_CHUNK = {"text": "Chapter 5: dollar-cost averaging means investing a fixed amount at regular "
                      "intervals regardless of price, per Graham's formula investing.", "chunk_index": 12}
UNRELATED_CHUNK = {"text": "Chapter 2: Mr. Market is a metaphor for the irrational, moody nature of "
                            "stock prices day to day.", "chunk_index": 3}

with mock.patch("app.routers.connect.LLMService") as MockLLM, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed:
    MockEmbed.return_value.search_item_fresh.return_value = [DCA_CHUNK]
    MockEmbed.return_value.keyword_search_item.return_value = []
    MockEmbed.return_value.fetch_chunks_by_index.return_value = {}
    MockLLM.return_value.chat_with_book.return_value = "Chapter 5 covers dollar-cost averaging in detail."
    r1 = c.post("/connect/chat", json=chat_payload(
        "crm_item_a", "I need to know everything the book says about dollar cost averaging", turn_id="crm_t1"))
check("turn 1 succeeds", r1.status_code == 200, f"{r1.status_code} {r1.text[:150]}")
check("the DCA chunk_index is now persisted for this (user, book)",
      12 in persisted_indexes("crm_a", "crm_item_a"))

# Turn 2: a differently-worded follow-up whose OWN fresh search returns
# something UNRELATED (simulating embedding drift) — before the fix, this
# alone would have been the model's entire context, and it had no way to
# know the DCA chunk existed.
with mock.patch("app.routers.connect.LLMService") as MockLLM2, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbed2:
    MockEmbed2.return_value.search_item_fresh.return_value = [UNRELATED_CHUNK]
    MockEmbed2.return_value.keyword_search_item.return_value = []
    # The union step re-fetches persisted indexes not already in this turn's
    # fresh hits — this is what carries the DCA chunk forward.
    MockEmbed2.return_value.fetch_chunks_by_index.return_value = {12: DCA_CHUNK["text"]}
    MockLLM2.return_value.chat_with_book.return_value = "Yes — Chapter 5 goes further into it."
    r2 = c.post("/connect/chat", json=chat_payload(
        "crm_item_a", "Is that all the book says about this?", turn_id="crm_t2",
        history=[{"role": "user", "content": "I need to know everything the book says about dollar cost averaging"},
                 {"role": "assistant", "content": "Chapter 5 covers dollar-cost averaging in detail."}]))
check("turn 2 succeeds", r2.status_code == 200, f"{r2.status_code} {r2.text[:150]}")

# The critical assertion: the DCA chunk's text was actually IN the context
# built for the LLM call this turn, not just sitting in the DB unused.
call_kwargs = MockLLM2.return_value.chat_with_book.call_args.kwargs
excerpts_seen = call_kwargs.get("excerpts", [])
check("turn 2's model call included the DCA chunk from turn 1 — this is the exact bug fix",
      any("dollar-cost averaging" in e for e in excerpts_seen), excerpts_seen)
check("turn 2's model call ALSO included this turn's own fresh (unrelated) hit",
      any("Mr. Market" in e for e in excerpts_seen), excerpts_seen)
check("fetch_chunks_by_index was asked for the persisted DCA index to backfill its text",
      12 in (MockEmbed2.return_value.fetch_chunks_by_index.call_args.args[-1]
             if MockEmbed2.return_value.fetch_chunks_by_index.call_args else []))
check("both chunk_indexes are now persisted for this conversation",
      {3, 12} <= persisted_indexes("crm_a", "crm_item_a"))


# ═══════════════════════════════════════════════════════════════════════
section("B — a vague follow-up searches with the PRIOR question's text, not the vague text itself")
# ═══════════════════════════════════════════════════════════════════════
u_b = mkuser("crm_b")
mkitem("crm_item_b", "crm_b")
as_user("crm_b")

with mock.patch("app.routers.connect.LLMService") as MockLLMb, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedb:
    MockEmbedb.return_value.search_item_fresh.return_value = []
    MockEmbedb.return_value.keyword_search_item.return_value = []
    MockEmbedb.return_value.fetch_chunks_by_index.return_value = {}
    MockLLMb.return_value.chat_with_book.return_value = "placeholder"
    r_b = c.post("/connect/chat", json=chat_payload(
        "crm_item_b", "is that all?", turn_id="crm_tb",
        history=[{"role": "user", "content": "what does the book say about margin of safety"},
                 {"role": "assistant", "content": "It explains margin of safety as a buffer against error."}]))
    # search_item_fresh may be called once (expanded query) or twice (expanded
    # + raw, only on a broad-coverage turn) — "is that all?" alone isn't
    # broad-coverage, so exactly one call is expected, and its query should
    # be the PRIOR question, not the literal vague text.
    search_calls = MockEmbedb.return_value.search_item_fresh.call_args_list
queried_texts = [call.kwargs.get("query") for call in search_calls]
check("turn succeeds", r_b.status_code == 200, f"{r_b.status_code} {r_b.text[:150]}")
check("the retrieval query was the PRIOR user question, not the literal vague follow-up text",
      "margin of safety" in " ".join(queried_texts) and "is that all" not in " ".join(queried_texts).lower(),
      queried_texts)


# ═══════════════════════════════════════════════════════════════════════
section("C — a broad 'everything about X' question searches with a wider top_k than a normal one")
# ═══════════════════════════════════════════════════════════════════════
u_c = mkuser("crm_c")
mkitem("crm_item_c", "crm_c")
as_user("crm_c")

with mock.patch("app.routers.connect.LLMService") as MockLLMc, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedc:
    MockEmbedc.return_value.search_item_fresh.return_value = []
    MockEmbedc.return_value.keyword_search_item.return_value = []
    MockEmbedc.return_value.fetch_chunks_by_index.return_value = {}
    MockLLMc.return_value.chat_with_book.return_value = "placeholder"
    c.post("/connect/chat", json=chat_payload(
        "crm_item_c", "What is compound interest?", turn_id="crm_tc_normal"))
    normal_top_k = [call.kwargs.get("top_k") for call in MockEmbedc.return_value.search_item_fresh.call_args_list]

with mock.patch("app.routers.connect.LLMService") as MockLLMc2, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedc2:
    MockEmbedc2.return_value.search_item_fresh.return_value = []
    MockEmbedc2.return_value.keyword_search_item.return_value = []
    MockEmbedc2.return_value.fetch_chunks_by_index.return_value = {}
    MockLLMc2.return_value.chat_with_book.return_value = "placeholder"
    c.post("/connect/chat", json=chat_payload(
        "crm_item_c", "I need to know everything the book says about compound interest", turn_id="crm_tc_broad"))
    broad_top_k = [call.kwargs.get("top_k") for call in MockEmbedc2.return_value.search_item_fresh.call_args_list]
    broad_reply_tokens = MockLLMc2.return_value.chat_with_book.call_args.kwargs.get("max_visible_tokens")

check("a normal question uses the default top_k", normal_top_k and all(k == 8 for k in normal_top_k), normal_top_k)
check("a broad-coverage question uses a wider top_k", broad_top_k and all(k > 8 for k in broad_top_k), broad_top_k)
check("a broad-coverage question gets a larger reply token budget than the default 600",
      broad_reply_tokens is not None and broad_reply_tokens > 600, broad_reply_tokens)


# ═══════════════════════════════════════════════════════════════════════
section("D — accumulated context is scoped per (user, book) — a second book starts with nothing inherited")
# ═══════════════════════════════════════════════════════════════════════
mkitem("crm_item_a2", "crm_a")  # same user as section A, a DIFFERENT book
as_user("crm_a")

with mock.patch("app.routers.connect.LLMService") as MockLLMd, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbedd:
    MockEmbedd.return_value.search_item_fresh.return_value = []
    MockEmbedd.return_value.keyword_search_item.return_value = []
    MockEmbedd.return_value.fetch_chunks_by_index.return_value = {}
    MockLLMd.return_value.chat_with_book.return_value = "placeholder"
    r_d = c.post("/connect/chat", json=chat_payload(
        "crm_item_a2", "Is that all?", turn_id="crm_td"))

check("a second, unrelated book for the SAME user has no persisted context yet",
      persisted_indexes("crm_a", "crm_item_a2") == set())
check("the first book's persisted context is untouched by the second book's request",
      {3, 12} <= persisted_indexes("crm_a", "crm_item_a"))


# ═══════════════════════════════════════════════════════════════════════
section("E — a keyword-only hit (no vector-search match) still reaches the model and gets persisted")
# ═══════════════════════════════════════════════════════════════════════
u_e = mkuser("crm_e")
mkitem("crm_item_e", "crm_e")
as_user("crm_e")

KEYWORD_CHUNK_TEXT = "Chapter 9: the concept of 'Mr. Market' first appears here, named explicitly."

with mock.patch("app.routers.connect.LLMService") as MockLLMe, \
     mock.patch("app.routers.connect.EmbeddingService") as MockEmbede:
    # Vector search finds nothing (simulating embedding drift on an exact
    # proper-noun phrase) — only the keyword pass finds the chunk, and it
    # comes back as a bare chunk_index (no text — that's fetched separately).
    MockEmbede.return_value.search_item_fresh.return_value = []
    MockEmbede.return_value.keyword_search_item.return_value = [7]
    MockEmbede.return_value.fetch_chunks_by_index.return_value = {7: KEYWORD_CHUNK_TEXT}
    MockLLMe.return_value.chat_with_book.return_value = "placeholder"
    r_e = c.post("/connect/chat", json=chat_payload(
        "crm_item_e", "What does the book say about Mr. Market?", turn_id="crm_te"))

check("turn succeeds", r_e.status_code == 200, f"{r_e.status_code} {r_e.text[:150]}")
check("fetch_chunks_by_index was called to resolve the keyword-only hit's text",
      MockEmbede.return_value.fetch_chunks_by_index.called)
fetch_call_indexes = (MockEmbede.return_value.fetch_chunks_by_index.call_args.args[-1]
                      if MockEmbede.return_value.fetch_chunks_by_index.call_args else [])
check("chunk_index 7 (keyword-only hit) was among the indexes resolved", 7 in fetch_call_indexes)
excerpts_seen_e = MockLLMe.return_value.chat_with_book.call_args.kwargs.get("excerpts", [])
check("the keyword-only chunk's actual text reached the model's excerpts",
      any("Mr. Market" in e for e in excerpts_seen_e), excerpts_seen_e)
check("chunk_index 7 is now persisted for this conversation", 7 in persisted_indexes("crm_e", "crm_item_e"))


print("\n" + "=" * 62)
if failures:
    print(f"RESULT: {len(failures)} check(s) FAILED: {failures}")
    sys.exit(1)
else:
    print("RESULT: all Connect chat retrieval-memory checks passed")
