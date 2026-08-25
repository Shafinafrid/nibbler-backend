"""
Retrieval augmentation for POST /connect/chat (Aug 2026).

Root cause this exists to fix: chat used to run ONE vector search per turn,
querying with ONLY that turn's raw message text, and handed the model
whatever top-8 chunks came back — nothing more. A follow-up like "is that
all?" embeds nothing like the original question, so it silently pulled a
DIFFERENT top-8 next turn, and the model — genuinely only seeing what that
search returned — would flatly deny content it had correctly cited one turn
earlier. (Observed live: a user asked about dollar-cost averaging in "The
Intelligent Investor," got a detailed, accurate answer, then asked "is that
all the book says about this?" and the model said no such discussion
existed — because the second search didn't surface it.)

Three additions, all working in terms of `chunk_index` (the integer Pinecone
already stamps on every vector at ingestion — see embedding_service.py's
`index_text`) so they compose with the accumulated per-conversation chunk
memory in connect.py rather than duplicating it:

  1. `expand_query`      — vague follow-ups get the PREVIOUS user question's
                            text substituted in, instead of being embedded
                            literally (a heuristic, not an LLM call — cheap,
                            and it's exactly what this failure needed).
  2. `salient_terms`      — feeds the lexical fallback net: pure string
                            processing (no tokenizer) that picks out the
                            topic words from a message. The actual re-chunk-
                            and-match step lives on EmbeddingService
                            (`keyword_search_item`) instead of here, since
                            that's the one class that already owns the
                            tiktoken-dependent `_chunk_text` and is what
                            every test mocks at the boundary.
  3. `is_broad_coverage_question` — detects "everything about X" / "is that
                            all" style intent so the caller can widen top_k
                            and run multiple query variants instead of one.
"""
import re
from typing import List, Optional


# ── Vague follow-up detection & substitution ────────────────────────────────

# Deliberately short and literal — these are near-content-free utterances
# that carry no retrieval signal of their own. A longer message that happens
# to start with one of these words (e.g. "is that all there is regarding
# margin of safety?") still has its own topic words to embed, so this only
# fires when the WHOLE message is essentially just the filler phrase.
_VAGUE_FOLLOWUP_PATTERNS = [
    r"^is that (all|it|everything)\??$",
    r"^(that'?s )?all\??$",
    r"^(tell me )?(more|everything)( (about it|about that|in detail))?\??$",
    r"^go on\??$",
    r"^(what|anything) else\??$",
    r"^(and|so) then\??$",
    r"^continue\??$",
    r"^keep going\??$",
    r"^really\??$",
    r"^are you sure\??$",
]
_VAGUE_FOLLOWUP_RE = re.compile(
    "|".join(f"(?:{p})" for p in _VAGUE_FOLLOWUP_PATTERNS), re.IGNORECASE
)


def _last_user_message(history: List[dict]) -> Optional[str]:
    for turn in reversed(history or []):
        if turn.get("role") == "user":
            content = (turn.get("content") or "").strip()
            if content:
                return content
    return None


def expand_query(message: str, history: List[dict]) -> str:
    """Return the text to actually EMBED for retrieval this turn.

    A vague follow-up ("is that all?", "tell me more") carries almost no
    topical signal on its own — embedding it literally searches for
    "vagueness" instead of the conversation's actual subject. When the
    message matches that pattern and a prior user turn exists, search on
    the prior question's text instead (the topic hasn't changed; the user
    is asking for MORE of it). Anything else is returned unchanged — this
    is a narrow, cheap heuristic, not a general rewrite of every query.
    """
    stripped = message.strip()
    if _VAGUE_FOLLOWUP_RE.match(stripped):
        prior = _last_user_message(history)
        if prior:
            return prior
    return message


# ── Broad/"everything about X" coverage detection ───────────────────────────

_BROAD_COVERAGE_RE = re.compile(
    r"\b(everything|all|in\s+detail|comprehensive|"
    r"every(thing|\s+detail)|whole|entire|full(y)?|complete(ly)?|"
    r"in\s+full|thorough(ly)?|exhaustive(ly)?)\b",
    re.IGNORECASE,
)


def is_broad_coverage_question(message: str) -> bool:
    """Heuristic only — no LLM call. False positives just mean a slightly
    wider search for an ordinary question, which is harmless; false
    negatives mean a genuinely broad question gets the normal top_k, which
    is the status quo today. Bias toward matching.

    Deliberately checked BEFORE `expand_query` substitution, and a pure
    vague-follow-up utterance ("is that all?", "tell me more") is EXCLUDED
    here even though it asks for more — it carries no topical signal of its
    own, so "broad" for it means widening the search around whatever
    `expand_query` substitutes in, not around its own (empty) content. The
    caller applies broad-mode based on the ORIGINAL topic once expansion has
    resolved what that topic actually is — see connect.py's
    `_gather_chat_excerpts`, which calls this on the raw message before
    substitution, then again mentally via the substituted query's own words
    if the prior question happened to be broad."""
    if _VAGUE_FOLLOWUP_RE.match(message.strip()):
        return False
    return bool(_BROAD_COVERAGE_RE.search(message))


# ── Keyword fallback net ─────────────────────────────────────────────────────

_STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "and", "or",
    "is", "are", "was", "were", "be", "been", "being", "this", "that",
    "it", "its", "with", "about", "what", "does", "do", "did", "say",
    "says", "said", "book", "tell", "me", "i", "you", "your", "my",
    "everything", "all", "detail", "details", "know", "need", "want",
}


def salient_terms(message: str, max_terms: int = 6) -> List[str]:
    """Non-stopword tokens (3+ chars) from the message, longest first — a
    deliberately simple stand-in for real noun-phrase extraction. Longest
    tokens tend to be the most topic-specific (e.g. "averaging" over "cost"),
    and capping the count keeps the keyword pass cheap on a long question.

    Pure string processing, no tokenizer — deliberately kept out of
    EmbeddingService so the keyword MATCH-INTENT decision (does this
    message have any salient terms at all?) doesn't require touching
    tiktoken. The actual re-chunk-and-match step that DOES need tiktoken is
    `EmbeddingService.keyword_search_item` — see its docstring for why that
    split matters."""
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", message.lower())
    terms = sorted(
        {w for w in words if len(w) >= 3 and w not in _STOPWORDS},
        key=len, reverse=True,
    )
    return terms[:max_terms]
