"""
Regression tests for the personalization audit fixes (Aug 2026).

Each test here corresponds to a specific audit finding and fails against the
pre-fix code. Plain script, no pytest — matches this repo's existing
convention (`for t in tests/test_*.py; do .venv/bin/python "$t"; done`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_test_personalization_audit.db")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print("PASS  %s" % label)
    else:
        failed += 1
        print("FAIL  %s" % label)


# ── Finding #1: option ids can no longer diverge from what was persisted ────
# Pre-fix, the personalize card round-tripped through the deck LLM and its
# option ids were re-stamped BY POSITION onto whatever came back. A model
# that reordered the options produced a card whose "opt0" meant a different
# answer than the "opt0" persisted on the row. The fix removes the round
# trip: the card is inserted server-side from the same object that is
# persisted, so there is no second list to diverge from.

from app.services.session_service import _insert_personalization_card

question = {
    "question": "Automate it, or do it by hand?",
    "eyebrow": "ONE QUICK QUESTION",
    "highlight": None,
    "options": [
        {"id": "opt0", "text": "Automate it", "tag": "prefers_automation"},
        {"id": "opt1", "text": "By hand", "tag": "prefers_manual_control"},
    ],
}

deck = {
    "cards": [
        {"kind": "hook", "title": "H", "body": "b"},
        {"kind": "insight", "title": "I", "body": "b"},
        {"kind": "prompt", "title": "P", "body": "b"},
        {"kind": "summary", "title": "S", "body": "b"},
    ],
}
_insert_personalization_card(deck, question)

cards = deck["cards"]
check("personalize card is inserted immediately before the summary",
      cards[-2]["kind"] == "personalize" and cards[-1]["kind"] == "summary")
check("the interaction card is still directly before it",
      cards[-3]["kind"] == "prompt")
check("the inserted card's options are the SAME OBJECTS that get persisted",
      cards[-2]["personalizeOptions"] is question["options"])
check("option ids on the card match the persisted ids exactly",
      [o["id"] for o in cards[-2]["personalizeOptions"]] == ["opt0", "opt1"])
check("opt0 still maps to the tag it was generated with",
      next(o for o in cards[-2]["personalizeOptions"] if o["id"] == "opt0")["tag"]
      == "prefers_automation")

# A model can no longer emit a personalize card at all — the kind is not in
# the schema enum, so an invented one is rejected before it can carry
# profile-mutating tags.
from app.services.llm.schemas import CARD_KINDS
check("'personalize' is not a model-emittable card kind",
      "personalize" not in CARD_KINDS)

# Degenerate deck shapes must not raise or produce a nonsensical position.
empty = {"cards": []}
_insert_personalization_card(empty, question)
check("an empty deck is left alone rather than raising", empty["cards"] == [])

no_summary = {"cards": [{"kind": "hook", "title": "H", "body": "b"}]}
_insert_personalization_card(no_summary, question)
check("a deck with no summary appends rather than inserting at -1",
      no_summary["cards"][-1]["kind"] == "personalize")

blank = {"cards": [{"kind": "hook", "title": "H"}, {"kind": "summary", "title": "S"}]}
_insert_personalization_card(blank, {"question": "", "options": []})
check("a malformed question is skipped entirely",
      all(c["kind"] != "personalize" for c in blank["cards"]))


# ── Finding #13: every non-empty pull-quote is grounded, regardless of length ─
from app.services.llm.validation import validate_personalization
from app.services.llm.errors import ProviderError

chunks = ["The author writes at length about compounding and patience."]

short_invented = {
    "question": "Q?", "eyebrow": "E",
    "options": [
        {"text": "a", "tag": "prefers_automation"},
        {"text": "b", "tag": "prefers_simplicity"},
    ],
    "highlight": "Stay curious.",   # 13 chars — under the old 25-char skip
}
try:
    validate_personalization(short_invented, provider="test", source_chunks=chunks)
    check("a short invented pull-quote is rejected", False)
except ProviderError:
    check("a short invented pull-quote is rejected", True)

short_real = dict(short_invented, highlight="compounding and patience")
try:
    validate_personalization(short_real, provider="test", source_chunks=chunks)
    check("a short but genuine pull-quote is accepted", True)
except ProviderError:
    check("a short but genuine pull-quote is accepted", False)

null_quote = dict(short_invented, highlight=None)
try:
    validate_personalization(null_quote, provider="test", source_chunks=chunks)
    check("a null pull-quote is accepted (most cards have none)", True)
except ProviderError:
    check("a null pull-quote is accepted (most cards have none)", False)


print()
print("Personalization audit fixes: %d/%d passed" % (passed, passed + failed))
sys.exit(1 if failed else 0)
