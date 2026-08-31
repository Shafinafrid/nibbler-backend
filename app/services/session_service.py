"""
Shared nibble-session generation.

Used by BOTH the on-demand HTTP handler (`POST /bites/session`) and the
scheduler that pre-generates the daily nibble(s) ~5 minutes before the user's
delivery time (see notification_service). Keeping one code path means the
"tap a book" flow and the "delivered at your time" flow produce identical decks.

This module is HTTP-agnostic: it raises SessionGenerationError (with a
suggested status_code) instead of FastAPI HTTPException, so the scheduler can
use it without a request context.
"""

import random
import re
import uuid
import logging
from datetime import date as date_cls
from typing import List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.models.bite import DailyBite
from app.models.library import LibraryItem
from app.models.personalization import PersonalizationQuestion
from app.models.user import User
from app.services.llm import LLMService
from app.services.embedding_service import EmbeddingService
from app.services import image_select
from app.services.entitlement_service import is_source_unlocked, touch_last_active

logger = logging.getLogger(__name__)

# read length → total cards in the deck / retrieval breadth / story words
CARD_TARGETS = {5: 5, 10: 8, 15: 12}
WISDOM_TOP_K = {5: 6, 10: 10, 15: 14}
STORY_WORDS = {5: 1100, 10: 2200, 15: 3300}

# ── Dynamic growth-profile personalization (Aug 2026) ───────────────────────
# Chance any single ELIGIBLE wisdom session also asks a grounded
# personalization question. Deliberately unseeded/probabilistic per session
# (not a fixed-N counter, not adaptive to profile confidence) — the founder's
# own framing was "every once in a while", and replay safety comes from the
# surrounding DailyBite per-day cache (this function is only ever reached
# once per (user, item, day) — see generate_session_for_item's docstring),
# not from the roll itself being deterministic.
PERSONALIZATION_PROBABILITY = 0.20
# Below this, the retrieved excerpts are too thin to ground a genuine
# preference question in the book's actual content, not just its topic.
PERSONALIZATION_MIN_CHUNK_CHARS = 400
PERSONALIZATION_MIN_CHUNKS = 3


def _roll_personalization(db: Session, user: User, item: LibraryItem, chunks: List[str]) -> bool:
    """Whether THIS wisdom session should also carry a personalization card.

    Called once per (user, item, day) from inside generate_session_for_item
    — the SAME function both the on-demand HTTP path and the scheduler's
    pre-generation path share, so scheduler-delivered nibbles get this
    feature too, not just on-demand taps. Must be called BEFORE card_target
    is finalized (the extra card has to be baked into the exact card count
    the schema enforces, not appended after generation)."""
    if len(chunks) < PERSONALIZATION_MIN_CHUNKS:
        return False
    if sum(len(c) for c in chunks) < PERSONALIZATION_MIN_CHUNK_CHARS:
        return False
    # Not the user's first session with this book — a specific, book-grounded
    # question reads better once the user has actually started the book, and
    # chunk_ids' progressive-coverage exclusion means session 2+ retrieves a
    # more book-specific slice than session 1's cold-start query.
    prior_exists = (
        db.query(DailyBite.id)
        .filter(DailyBite.user_id == user.id, DailyBite.library_item_id == item.id)
        .first()
        is not None
    )
    if not prior_exists:
        return False
    return random.random() < PERSONALIZATION_PROBABILITY


def _insert_personalization_card(result: dict, question: dict) -> None:
    """Splice the already-validated personalization card into a generated
    deck, in place, immediately before the summary card.

    Server-side insertion is deliberate — see the long note at the call
    site. The card object placed here carries the SAME option list (ids
    included) that gets persisted on the PersonalizationQuestion row, so
    "opt0" on screen and "opt0" in the database are the same option by
    construction rather than by trusting a model to preserve order.

    Never raises: personalization is an occasional bonus card, and a
    malformed deck shape must degrade to an ordinary (still perfectly
    valid) session rather than cost the user their nibble.
    """
    cards = result.get("cards")
    if not isinstance(cards, list) or not cards:
        return
    card = {
        "kind": "personalize",
        "eyebrow": question.get("eyebrow") or "ONE QUICK QUESTION",
        "title": question.get("question") or "",
        "body": None,
        "highlight": question.get("highlight"),
        "options": None,
        "explanation": None,
        "personalizeOptions": question.get("options") or [],
    }
    if not card["title"] or not card["personalizeOptions"]:
        return
    # Before the summary when there is one (the normal case — validate_wisdom
    # guarantees it); otherwise append, so an unexpected deck shape still
    # yields a coherent deck rather than a card in a nonsensical position.
    if cards[-1].get("kind") == "summary":
        cards.insert(len(cards) - 1, card)
    else:
        cards.append(card)


class SessionGenerationError(Exception):
    """A session couldn't be generated (bad input, retrieval empty, provider failure)."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _slice_words(text: str, start: int, count: int) -> str:
    """The slice of `text` covering words [start, start+count) — with every
    space, line break and blank line between them left exactly as written.

    Story progress is stored as a word offset, and the old implementation
    resolved it with `" ".join(text.split()[a:b])`, which threw away every
    paragraph break in the book before the text was ever shown to the reader.
    Word offsets stay valid here: `\\S+` tokenises identically to `str.split()`.
    """
    spans = [m.span() for m in re.finditer(r"\S+", text)]
    if start >= len(spans):
        return ""
    end = min(start + count, len(spans))
    return text[spans[start][0]:spans[end - 1][1]]


def _paragraphs(text: str) -> List[str]:
    """Blank-line-separated paragraphs, each with its internal line breaks
    (dialogue, verse, lists) intact."""
    return [p.strip("\n") for p in re.split(r"\n[ \t]*\n", text) if p.strip()]


def split_story_cards(excerpt: str, card_target: int) -> List[str]:
    """Cut today's portion into `card_target` card bodies at paragraph
    boundaries, balanced by word count. Purely mechanical — no model touches
    the text, so what the reader sees is byte-for-byte the author's."""
    paras = _paragraphs(excerpt)
    if not paras:
        return []
    if len(paras) <= card_target:
        return paras

    counts = [len(p.split()) for p in paras]
    per_card = sum(counts) / card_target
    cards: List[str] = []
    cur: List[str] = []
    cur_words = 0
    for i, para in enumerate(paras):
        cur.append(para)
        cur_words += counts[i]
        remaining_paras = len(paras) - i - 1
        remaining_cards = card_target - len(cards) - 1
        # Close this card once it has its share — unless the paragraphs left
        # are only just enough to fill the cards left.
        if remaining_cards > 0 and (
            cur_words >= per_card or remaining_paras <= remaining_cards
        ):
            cards.append("\n\n".join(cur))
            cur, cur_words = [], 0
    if cur:
        cards.append("\n\n".join(cur))
    return cards


def _profile_query(profile: dict) -> str:
    query_bits = [
        profile.get("aspirationUnderstanding") or profile.get("aspirationLabel") or "",
        " ".join(profile.get("interests") or []),
        profile.get("lifeArea") or "",
    ]
    return " ".join(b for b in query_bits if b).strip()


def generate_session_for_item(
    db: Session,
    *,
    user: User,
    item: LibraryItem,
    read_length: int,
    profile: dict,
    today: date_cls,
    origin: str = "manual",
) -> DailyBite:
    """
    Generate and persist one nibble session for (user, item, today), returning
    the DailyBite. If a concurrent write already created it (unique index on
    user/item/date), returns the existing winner instead of raising.

    Does NOT enforce daily caps or dedupe an already-generated session — callers
    own those pre-checks (the HTTP handler and the scheduler differ there).
    """
    if not is_source_unlocked(user, item):
        # Belt-and-suspenders: both callers (the HTTP handler and the
        # scheduler) already filter locked sources out before reaching here,
        # but this is the ONE place that actually calls the paid LLM, so it
        # is also the one place that must refuse unconditionally rather than
        # trust every caller to have checked (Task 2).
        raise SessionGenerationError("This source is locked for Free accounts.", status_code=403)
    read_length = read_length if read_length in CARD_TARGETS else 5
    # Which model generates this deck is a routing decision, not a tier one:
    # free, trial and premium users all get whatever LLM_ROUTING_MODE selects.
    llm = LLMService()
    mode = item.mode or "wisdom"
    card_target = CARD_TARGETS[read_length]
    story_finished = False
    goal_passage = None
    chunk_ids = None

    if mode == "story":
        words = (item.content or "").split()
        if not words:
            raise SessionGenerationError("No readable text stored for this book.", 422)
        progress = item.story_progress or 0
        if progress >= len(words):
            story_finished = True
            result = {
                "title": "The end — you finished it!",
                "chapter": "THE END",
                "headline": f"You've read all of {item.title}.",
                "preview": "Every last page, one daily portion at a time.",
                "cards": [{
                    "kind": "summary",
                    "eyebrow": "THE END",
                    "title": f"You finished {item.title}.",
                    "body": "That's the whole book — read the way books are meant to be read: steadily, in order, without losing the thread.\n\nAdd another story to your library to start your next journey.",
                }],
                "quiz": None,
            }
        else:
            n = STORY_WORDS[read_length]
            excerpt = _slice_words(item.content or "", progress, n)
            part_number = progress // n + 1
            bodies = split_story_cards(excerpt, max(3, card_target - 1))
            if not bodies:
                raise SessionGenerationError("No readable text stored for this book.", 422)
            # Figures the reader has already reached. A picture from further
            # ahead is a spoiler — a character, a place, a plot beat they have
            # not met — so the shortlist is capped at today's position and the
            # same cap is re-applied after the model answers.
            # Both sides of this comparison are WORD fractions of the same
            # text: `progress` is a word offset into item.content, and a
            # candidate's position is the fraction of the book's words before
            # it. Candidates recorded in pages or spine units are refused
            # outright rather than converted — see image_select.
            story_max_position = min(1.0, (progress + n) / max(1, len(words)))
            story_candidates = image_select.safe_shortlist(
                item.images, excerpt, max_position=story_max_position,
            )

            # The model never carries the prose — it only names what it reads.
            # Story mode's whole promise is the book itself, so a paraphrase or
            # a silently reflowed paragraph is a bug, not a style choice.
            try:
                meta = llm.generate_story_metadata(
                    book_title=item.title, author=item.author,
                    card_bodies=bodies, part_number=part_number,
                    image_options=image_select.safe_prompt(story_candidates),
                )
            except Exception as e:
                logger.warning("Story metadata failed (%s) — serving plain headings", e)
                meta = {}
            headings = meta.get("headings") or []
            story_image_ids = meta.get("imageIds") or []
            result = {
                "title": meta.get("title") or f"{item.title} — part {part_number}",
                "chapter": f"PART {part_number}",
                "headline": meta.get("headline") or "Today's portion of your book.",
                "preview": meta.get("preview") or "",
                "cards": [
                    {
                        "kind": "story",
                        "eyebrow": "TODAY'S READING" if i == 0 else "THE STORY CONTINUES",
                        "title": headings[i] if i < len(headings) else "",
                        "body": body,
                        # Positional: story cards are server-owned and have no
                        # id the model could name, so it answers with an array
                        # parallel to `headings`. Validated below.
                        "imageId": (story_image_ids[i]
                                    if i < len(story_image_ids) else None),
                    }
                    for i, body in enumerate(bodies)
                ],
                "quiz": None,
            }
            # Failure here must never cost the reader their portion: the text
            # is already correct and complete, and a picture is a garnish.
            try:
                image_select.attach_images(
                    result["cards"], shortlisted=story_candidates,
                    user_id=user.id, item_id=item.id,
                    max_position=story_max_position,
                )
            except Exception as e:
                logger.warning("Story image attach failed (%s) — text-only portion", e)
                for card in result["cards"]:
                    card.pop("imageId", None)
            item.story_progress = min(progress + n, len(words))
    else:
        profile = profile or {}
        pq = _profile_query(profile)
        query = pq or item.title
        embeddings = EmbeddingService()

        # Progressive coverage: exclude every chunk this user's previous
        # sessions already drew from, so each nibble explores NEW ground —
        # without this, the same profile query returned the same top-K chunks
        # every single day, and 'Explored %' could never honestly grow.
        served: set = set()
        for (ids,) in (
            db.query(DailyBite.chunk_ids)
            .filter(
                DailyBite.user_id == user.id,
                DailyBite.library_item_id == item.id,
                DailyBite.chunk_ids.isnot(None),
            )
            .all()
        ):
            served.update(i for i in (ids or []) if isinstance(i, int))

        try:
            fresh = embeddings.search_item_fresh(
                query=query, user_id=user.id, item_id=item.id,
                top_k=WISDOM_TOP_K[read_length],
                exclude_indexes=sorted(served),
            )
        except Exception as e:
            logger.warning("Fresh retrieval failed (%s) — raw-text fallback", e)
            fresh = []
        chunks = [f["text"] for f in fresh if f.get("text")]
        chunk_ids = [f["chunk_index"] for f in fresh if isinstance(f.get("chunk_index"), int)]
        # Retrieval is ranked by similarity to the growth profile, so the top
        # chunk IS today's most goal-relevant passage (Connect tab uses it).
        if chunks and pq:
            goal_passage = " ".join(chunks[0].split())
            goal_passage = goal_passage[:280] + ("…" if len(goal_passage) > 280 else "")
        if not chunks and item.content:
            chunks = [item.content[:8000]]  # Pinecone down — fall back to raw text
            chunk_ids = []
        if not chunks:
            raise SessionGenerationError("No indexed content found for this item.", 422)
        # Figures whose caption/alt/nearby text overlaps the passages this
        # deck is being written from. Empty for most books, which is the
        # expected outcome — a text-only deck is a correct deck.
        wisdom_candidates = image_select.safe_shortlist(item.images, " ".join(chunks))

        # Dynamic growth-profile personalization (Aug 2026): decided BEFORE
        # card_target is finalized, and generated in its own call BEFORE the
        # deck call, so its grounding is validated once against these same
        # excerpts (see llm.generate_personalization_question) rather than
        # trusted to the deck model re-deriving it faithfully. `chunk_ids`
        # here excludes the raw-text fallback (chunk_ids == [] in that case),
        # which is correct — a personalization question should only ever be
        # grounded in real retrieved passages, never the 8000-char fallback.
        personalization_question = None
        if chunk_ids and _roll_personalization(db, user, item, chunks):
            personalization_question = llm.generate_personalization_question(
                book_title=item.title, author=item.author,
                profile=profile, context_chunks=chunks,
            )
            if personalization_question:
                # Stamp a stable, purely positional id onto each option.
                # "optN" by array index — never something a model invents
                # (personalization_option_schema has no id field at all).
                # This exact list, ids included, is BOTH persisted on the
                # PersonalizationQuestion row below AND spliced verbatim into
                # the deck after generation, so the id the user taps and the
                # id the answer endpoint resolves are the same object.
                for i, opt in enumerate(personalization_question.get("options") or []):
                    opt["id"] = f"opt{i}"

        try:
            # NOTE (audit fix, Aug 2026): the deck is generated at its NORMAL
            # card count and is told nothing about personalization. The card
            # is inserted server-side, below.
            #
            # The original implementation asked the deck model to reproduce a
            # pre-generated personalize card verbatim at a pinned position,
            # then re-stamped optN ids onto whatever came back BY POSITION.
            # That silently assumed the model preserves option ORDER, and
            # nothing enforced it — validate_wisdom checked option
            # count/text/tag validity but never equality with the original —
            # so a model that reordered the options produced a card where
            # "opt0" on screen meant a different answer than "opt0" in the
            # persisted row. The user's tap then applied the wrong profile
            # tag: silent, plausible, and invisible from the outside.
            #
            # Splicing server-side removes the failure mode rather than
            # trying to detect it: the card the user sees IS the object that
            # was grounding-validated and persisted, not a model's copy.
            result = llm.generate_wisdom_session(
                book_title=item.title, author=item.author,
                profile=profile, context_chunks=chunks,
                card_target=card_target, read_length=read_length,
                image_options=image_select.safe_prompt(wisdom_candidates),
            )
        except Exception as e:
            raise SessionGenerationError(f"Session generation failed: {e}", 502)

        if personalization_question:
            _insert_personalization_card(result, personalization_question)

        # Ownership, book and shortlist membership are all re-checked here from
        # the stored rows — the id came back from a model, so nothing about it
        # is trusted. An exception must not lose an otherwise valid deck.
        try:
            image_select.attach_images(
                result.get("cards") or [], shortlisted=wisdom_candidates,
                user_id=user.id, item_id=item.id,
            )
        except Exception as e:
            logger.warning("Wisdom image attach failed (%s) — text-only deck", e)
            for card in result.get("cards") or []:
                if isinstance(card, dict):
                    card.pop("imageId", None)

    bite = DailyBite(
        id=str(uuid.uuid4()),
        user_id=user.id,
        library_item_id=item.id,
        date=today,
        title=(result.get("title") or item.title)[:250],
        insight=result.get("preview") or result.get("headline") or "",
        reflection="",
        action="",
        source=item.title,
        theme="story_finished" if story_finished else mode,
        cards=result.get("cards") or [],
        quiz=result.get("quiz"),
        read_length=read_length,
        mode=mode,
        chapter=(result.get("chapter") or "")[:250],
        headline=(result.get("headline") or "")[:500],
        preview=result.get("preview") or "",
        goal_passage=goal_passage,
        chunk_ids=chunk_ids,
        origin=origin,
    )
    db.add(bite)
    # Same transaction as the DailyBite insert — if the personalization card
    # made it into `result["cards"]` above, its question row must exist
    # atomically with the deck that references it, or a crash between the
    # two would leave a deck the answer endpoint can never find a row for.
    # `daily_bite_id` is unique (PersonalizationQuestion.__table_args__), so
    # this can never create two rows for one bite.
    if mode != "story" and personalization_question:
        db.add(PersonalizationQuestion(
            user_id=user.id,
            daily_bite_id=bite.id,
            library_item_id=item.id,
            profile_id=(profile or {}).get("id"),
            question=personalization_question.get("question") or "",
            options=personalization_question.get("options") or [],
            source_chunk_ids=chunk_ids,
        ))
    # A session was actually generated for this source — real "use", the
    # authoritative signal the deterministic downgrade fallback ranks on
    # (see entitlement_service._fallback_candidates), distinct from a bare
    # metadata edit that also bumps `updated_at`.
    touch_last_active(item)
    try:
        db.commit()
    except IntegrityError:
        # Unique index on (user, item, date): a concurrent request won — return
        # it. Our own personalization_question (if any) is simply discarded
        # here along with `bite` — it belongs to the losing attempt, not the
        # bite we're about to return, and creating a row against a
        # daily_bite_id we don't own would be a correctness bug, not a
        # harmless duplicate (that FK belongs to whichever attempt actually won).
        db.rollback()
        winner = db.query(DailyBite).filter(
            DailyBite.user_id == user.id,
            DailyBite.library_item_id == item.id,
            DailyBite.date == today,
        ).first()
        if winner:
            return winner
        raise
    db.refresh(bite)
    return bite
