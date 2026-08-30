"""
The rules a JSON Schema cannot state, checked in one place for every provider.

A schema can say "exactly five cards, each with a kind from this enum". It
cannot say "the FIRST card is a hook, the LAST is a summary, and the one before
the last is a quiz" — order-dependent constraints are outside JSON Schema, and
"exactly one option is `correct: true`" is outside the strict subset too. Those
are exactly the rules whose violation ships a broken deck to a paying user, so
they are enforced here, after parsing and before persistence.

Everything raises `ProviderError`, never a bare exception, so the router can
tell a structurally bad response (retry once, then fall back) from a refusal
(stop). The distinction between the two categories:

  * `SCHEMA`     — could not be turned into the expected JSON at all.
  * `VALIDATION` — parsed fine, but the content breaks a product rule.

Both are eligible for one same-provider retry and then fallback: a model that
returns a four-card deck when five were asked for will often get it right on a
second pass, and a different model is a bigger hammer than a second attempt.
"""

import json
import random
import re
from typing import Any, Dict, List, Optional

from .errors import ErrorCategory, ProviderError
from .jsonschema_lite import schema_errors
from .schemas import CARD_KINDS, PERSONALIZATION_TAGS

# Card kinds that may fill the middle of a deck. Hook and summary are pinned to
# the ends, and the interaction card is requested explicitly, so everything
# else has to be an insight.
_MIDDLE_KINDS = {"insight"}


def _fail(provider: str, detail: str, category: str = ErrorCategory.VALIDATION) -> None:
    raise ProviderError(category, provider, detail)


def parse_json_loose(text: str, provider: str) -> Dict[str, Any]:
    """Best-effort JSON out of a text response.

    Only needed on the paths where a provider returned prose instead of using
    its structured-output mechanism — a fenced block, or JSON with an
    apologetic sentence in front of it. Strict/tool paths never reach this.
    """
    clean = (text or "").strip()
    if not clean:
        _fail(provider, "empty response body", ErrorCategory.EMPTY_RESPONSE)
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean)
    start, end = clean.find("{"), clean.rfind("}")
    if start >= 0 and end > start:
        clean = clean[start:end + 1]
    try:
        parsed = json.loads(clean)
    except Exception as e:
        _fail(provider, "unparseable JSON (%s)" % type(e).__name__, ErrorCategory.SCHEMA)
    if not isinstance(parsed, dict):
        _fail(provider, "top-level JSON is %s, expected object" % type(parsed).__name__,
              ErrorCategory.SCHEMA)
    return parsed


def enforce_schema(data: Dict[str, Any], schema: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """Check a parsed response against the schema we asked for.

    Runs on EVERY provider, not just the ones we distrust. Supplying a schema
    and receiving a response are two different events: Haiku treats the schema
    as a tool hint, Qwen's grammar coverage depends on the llama-server build,
    and all three have a text fallback path that bypasses structured output
    entirely. Without this, a missing key surfaces as a KeyError in the mobile
    app instead of a caught, retryable failure here.
    """
    problems = schema_errors(data, schema)
    if problems:
        # Only the first three, and only structural descriptions — these are
        # paths and type names, never response content.
        _fail(provider, "response does not match schema: " + "; ".join(problems[:3]),
              ErrorCategory.SCHEMA)
    return data


# ── Wisdom ──────────────────────────────────────────────────────────────────

def _normalize_for_grounding(text: str) -> str:
    """Collapse whitespace and unify quote glyphs for substring comparison.

    A model reproducing a quotation faithfully may still normalise a curly
    apostrophe or re-wrap a line. Those are not fabrications, and failing them
    would make the grounding check fire constantly and get switched off — which
    is worse than not having it.
    """
    lowered = (text or "").lower()
    for fancy, plain in (("‘", "'"), ("’", "'"), ("“", '"'),
                         ("”", '"'), ("–", "-"), ("—", "-"),
                         ("…", "...")):
        lowered = lowered.replace(fancy, plain)
    return " ".join(lowered.split())


# Below this length a "quote" is a fragment that could coincidentally appear
# anywhere, so matching it proves nothing and failing it proves less.
MIN_GROUNDED_QUOTE_CHARS = 25

def _validate_options(provider: str, options: Any, where: str) -> None:
    """Four options, exactly one correct.

    "Exactly one" is the rule that matters: zero correct answers makes the quiz
    unanswerable, and two makes it unfair. Neither is expressible in the strict
    schema subset, and both have shipped from real models.
    """
    if not isinstance(options, list) or len(options) != 4:
        _fail(provider, "%s: expected 4 options, got %s" % (
            where, len(options) if isinstance(options, list) else type(options).__name__))
    correct = 0
    for opt in options:
        if not isinstance(opt, dict) or not str(opt.get("text") or "").strip():
            _fail(provider, "%s: option missing text" % where)
        if opt.get("correct") is True:
            correct += 1
    if correct != 1:
        _fail(provider, "%s: %d correct options, expected exactly 1" % (where, correct))


def _validate_personalize_options(provider: str, options: Any, where: str) -> None:
    """2-4 personalization options, each with non-empty text and a tag from
    the fixed PERSONALIZATION_TAGS vocabulary. Unlike quiz options, there is
    no "exactly one correct" rule — these are preference choices, not a
    right/wrong quiz — and the tag is what a deterministic, code-owned
    mapping (PERSONALIZATION_TAG_DELTAS, in profile-delta application code)
    turns into an actual growth-profile shift, never a number the model
    invents itself. See schemas.py's personalization_option_schema docstring
    for why a closed enum matters here."""
    if not isinstance(options, list) or not (2 <= len(options) <= 4):
        _fail(provider, "%s: expected 2-4 personalize options, got %s" % (
            where, len(options) if isinstance(options, list) else type(options).__name__))
    for opt in options:
        if not isinstance(opt, dict) or not str(opt.get("text") or "").strip():
            _fail(provider, "%s: personalize option missing text" % where)
        if opt.get("tag") not in PERSONALIZATION_TAGS:
            _fail(provider, "%s: personalize option has unsupported tag %r" % (where, opt.get("tag")))


def validate_personalization(
    question: Dict[str, Any],
    *,
    provider: str,
    source_chunks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Semantic check for the standalone personalization-question call
    (generate_personalization_question), BEFORE its result is spliced into
    the deck-generation prompt (see session_service._roll_personalization).
    Grounding is checked here, not just left to validate_wisdom's later
    pass, because this is the call that actually derived the question from
    the book's excerpts — validate_wisdom's own check on the same
    `highlight` field afterward is defense in depth against the deck model
    disobeying the instruction to reproduce it verbatim, not a substitute
    for checking it at the source."""
    if not str(question.get("question") or "").strip():
        _fail(provider, "personalization question is empty")
    if not str(question.get("eyebrow") or "").strip():
        _fail(provider, "personalization question missing eyebrow")
    _validate_personalize_options(provider, question.get("options"), "personalization question")

    quote = str(question.get("highlight") or "").strip()
    if source_chunks and len(quote) >= MIN_GROUNDED_QUOTE_CHARS:
        haystack = _normalize_for_grounding("\n".join(source_chunks))
        if _normalize_for_grounding(quote) not in haystack:
            _fail(provider, "personalization highlight quote does not appear in the source")

    return question


def validate_wisdom(
    session: Dict[str, Any],
    *,
    provider: str,
    card_target: int,
    quiz_target: int,
    interaction_kind: str,
    source_chunks: Optional[List[str]] = None,
    has_personalization: bool = False,
) -> Dict[str, Any]:
    """Full semantic check of a Wisdom deck. Returns the session unchanged.

    `interaction_kind` is "quiz" or "prompt" — whichever the user's contentMode
    asked for. It is checked rather than assumed because the deck's shape is a
    personalization promise: an analytical learner was told they'd be quizzed.

    `source_chunks` are the excerpts the deck was built from. When supplied,
    every `highlight` pull-quote is checked to actually OCCUR in them. The
    prompt says to reproduce quotations exactly; this is the part that finds
    out whether it did. A pull-quote is rendered to the user as the book's own
    words, so an invented one is the product's core promise breaking silently —
    and a prompt instruction is not a guarantee.

    `has_personalization` (Aug 2026): true when session_service rolled a
    dynamic growth-profile question into this deck's card_target. When true,
    the tail shape gains one more pinned position — [..., interaction_card,
    "personalize", "summary"] instead of [..., interaction_card, "summary"] —
    since the personalize card's own question/options were pre-generated and
    validated separately (see validate_personalization) and are handed to
    THIS call already spliced into `cards` by the prompt; this function only
    confirms the deck-generation model actually placed it where asked.
    """
    for key in ("title", "headline", "preview"):
        if not str(session.get(key) or "").strip():
            _fail(provider, "missing %s" % key)

    cards = session.get("cards")
    if not isinstance(cards, list) or not cards:
        _fail(provider, "no cards", ErrorCategory.SCHEMA)
    if len(cards) != card_target:
        _fail(provider, "expected %d cards, got %d" % (card_target, len(cards)))

    for i, card in enumerate(cards):
        if not isinstance(card, dict):
            _fail(provider, "card %d is not an object" % i, ErrorCategory.SCHEMA)
        kind = card.get("kind")
        if kind not in CARD_KINDS:
            _fail(provider, "card %d has unsupported kind %r" % (i, kind))
        # The app renders eyebrow + title on every card; a blank one is a
        # visibly broken card, not a cosmetic issue.
        if not str(card.get("title") or "").strip():
            _fail(provider, "card %d has no title" % i)
        if kind == "quiz":
            _validate_options(provider, card.get("options"), "card %d" % i)
        elif kind == "personalize":
            _validate_personalize_options(provider, card.get("personalizeOptions"), "card %d" % i)
        elif not str(card.get("body") or "").strip():
            _fail(provider, "card %d (%s) has no body" % (i, kind))

    if cards[0].get("kind") != "hook":
        _fail(provider, "first card is %r, expected hook" % cards[0].get("kind"))
    if cards[-1].get("kind") != "summary":
        _fail(provider, "last card is %r, expected summary" % cards[-1].get("kind"))

    # tail_offset: how far from the end the interaction card sits. Normally
    # it's -2 (right before summary); with a personalize card also pinned
    # to -2, the interaction card is pushed one further back to -3.
    if has_personalization:
        if len(cards) < 2 or cards[-2].get("kind") != "personalize":
            _fail(provider, "expected personalize card at position -2")
        tail_offset = 3
    else:
        tail_offset = 2

    if len(cards) >= tail_offset + 1:
        interaction_index = -tail_offset
        got = cards[interaction_index].get("kind")
        if got != interaction_kind:
            _fail(provider, "interaction card is %r, expected %r" % (got, interaction_kind))
        for i, card in enumerate(cards[1:interaction_index], start=1):
            if card.get("kind") not in _MIDDLE_KINDS:
                _fail(provider, "card %d is %r, expected insight" % (i, card.get("kind")))

    if source_chunks:
        haystack = _normalize_for_grounding("\n".join(source_chunks))
        for i, card in enumerate(cards):
            quote = str(card.get("highlight") or "").strip()
            if len(quote) < MIN_GROUNDED_QUOTE_CHARS:
                continue
            if _normalize_for_grounding(quote) not in haystack:
                _fail(provider,
                      "card %d has a highlight quote that does not appear in the source" % i)

    quiz = session.get("quiz")
    if not isinstance(quiz, list) or len(quiz) != quiz_target:
        _fail(provider, "expected %d review questions, got %s" % (
            quiz_target, len(quiz) if isinstance(quiz, list) else type(quiz).__name__))
    for i, q in enumerate(quiz):
        if not isinstance(q, dict) or not str(q.get("question") or "").strip():
            _fail(provider, "review question %d has no text" % i)
        _validate_options(provider, q.get("options"), "review question %d" % i)

    return session


def shuffle_quiz_options(session: Dict[str, Any]) -> Dict[str, Any]:
    """Randomize option order, in place, AFTER validation has passed.

    Models have a well-documented positional bias for the correct answer —
    users reported ~8 of 9 landing in slot B — and it is a property of
    generation that prompting does not reliably fix. Shuffling here covers both
    surfaces at once: the in-deck quiz card (today) and the standalone review
    array (replayed tomorrow), since both come from this one generation.

    Order matters: shuffling before validation would scramble the very
    positions an error message needs to point at.
    """
    for card in session.get("cards") or []:
        if card.get("kind") == "quiz" and card.get("options"):
            random.shuffle(card["options"])
    for q in session.get("quiz") or []:
        if q.get("options"):
            random.shuffle(q["options"])
    return session


# ── Story ───────────────────────────────────────────────────────────────────

def validate_story(meta: Dict[str, Any], *, provider: str, card_count: int) -> Dict[str, Any]:
    """Titling for a Story portion.

    One heading per card, exactly. A mismatch is not cosmetic: the cards are
    zipped with the headings by index, so an off-by-one silently labels every
    card with the previous card's heading.
    """
    headings = meta.get("headings")
    if not isinstance(headings, list):
        _fail(provider, "headings is %s, expected list" % type(headings).__name__,
              ErrorCategory.SCHEMA)
    if len(headings) != card_count:
        _fail(provider, "expected %d headings, got %d" % (card_count, len(headings)))
    if not str(meta.get("title") or "").strip():
        _fail(provider, "missing title")
    meta["headings"] = [str(h) for h in headings]
    return meta


# ── Aspiration ──────────────────────────────────────────────────────────────

_ASPIRATION_ENUMS = {
    "contentMode": {"analytical", "reflective", "practical"},
    "motivation": {"career", "skill", "habit", "curiosity", "prep"},
    "motivationType": {"intrinsic", "instrumental", "mixed"},
    "goalOrientation": {"mastery", "summary", "application"},
}


def validate_aspiration(result: Dict[str, Any], *, provider: str) -> Dict[str, Any]:
    """The onboarding profile seed.

    The clarification pairing is the rule worth having: if the model decided it
    needs to ask the user something, an empty `clarifyPrompt` leaves onboarding
    showing a blank question with no way forward.
    """
    if not isinstance(result.get("needsClarification"), bool):
        _fail(provider, "needsClarification is not a boolean")
    if result["needsClarification"] and not str(result.get("clarifyPrompt") or "").strip():
        _fail(provider, "needsClarification is true but clarifyPrompt is empty")
    for key, allowed in _ASPIRATION_ENUMS.items():
        if result.get(key) not in allowed:
            _fail(provider, "%s is %r, not one of %s" % (key, result.get(key), sorted(allowed)))
    if not str(result.get("lifeArea") or "").strip():
        _fail(provider, "missing lifeArea")
    interests = result.get("interests")
    if not isinstance(interests, list) or not interests:
        _fail(provider, "interests is empty")
    result["interests"] = [str(i) for i in interests]
    return result


# ── Connect ─────────────────────────────────────────────────────────────────

def validate_chat_reply(text: Optional[str], *, provider: str) -> str:
    """Connect is plain text; the only invalid answer is no answer."""
    reply = (text or "").strip()
    if not reply:
        _fail(provider, "empty chat reply", ErrorCategory.EMPTY_RESPONSE)
    return reply


def coerce_card_list(cards: Any) -> List[Dict[str, Any]]:
    """Drop the null placeholders the strict schema forces onto every card.

    The schema requires all seven card keys on all five card kinds, so a hook
    card arrives carrying `options: null` and `explanation: null`. Those nulls
    would be persisted into `daily_bites.cards` and shipped to a client that
    has never seen them, so they are stripped here — the stored deck keeps the
    exact shape it had before this refactor.
    """
    out: List[Dict[str, Any]] = []
    for card in cards or []:
        out.append({k: v for k, v in card.items() if v is not None})
    return out
