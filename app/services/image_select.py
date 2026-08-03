"""
Choosing which extracted picture — if any — belongs on a card.

The shape of this is deliberate. The model does NOT get a list of images and
free rein; it gets a bounded shortlist of opaque ids with a little text each,
may name at most one per card, and the server then re-checks that choice from
scratch against the database. The model's answer is a suggestion, never an
authorisation.

That split exists because the failure modes are asymmetric. A missed figure is
invisible. A picture from another user's book on your card is a data breach; a
picture from chapter 20 during chapter 3 is a spoiler that cannot be un-seen.
So every check the model could influence is repeated server-side, from the
stored candidate rows rather than from anything the model returned.

Four rules follow from that:

  * **Ids only.** The model never sees an S3 key, a URL, a filename or a
    presigned link, so it cannot construct one. A returned id that was not in
    the shortlist we handed it is rejected outright.
  * **Bounded.** At most MAX_SHORTLIST candidates per request. An unbounded
    list is prompt cost, and a long one degrades the choice anyway.
  * **Story never looks ahead.** Candidates past the reader's position are
    filtered out BEFORE the model sees them, and the position is checked again
    after it answers.
  * **No image is always valid.** Every path here can return None, and a card
    with no picture is the expected outcome for most cards in most books.
"""

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Enough for a real choice, small enough to stay cheap in the prompt.
MAX_SHORTLIST = 8
# Below this overlap a candidate is not "the picture for this card", it is
# merely a picture from the same book. Attaching those is what makes the
# feature feel random, so the floor is a quality gate, not an optimisation.
MIN_RELEVANCE = 2
# Bounded text per candidate in the prompt.
MAX_DESC_CHARS = 220

_WORD = re.compile(r"[a-z0-9']+")
# Words that match everything and therefore discriminate nothing.
_STOPWORDS = frozenset("""
a an and are as at be been but by can did do does for from had has have he her
his how i if in into is it its of on or our out she so than that the their them
then there these they this to too was we were what when which who will with you
your figure fig table chart diagram page chapter
""".split())


def _tokens(text: Optional[str]) -> set:
    return {w for w in _WORD.findall((text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _candidate_text(image: Dict[str, Any]) -> str:
    """Everything known about a candidate, as one searchable blob."""
    return " ".join(str(image.get(k) or "") for k in
                    ("caption", "alt", "chapter", "context"))


def relevance(image: Dict[str, Any], query_text: str) -> int:
    """Shared distinctive words between a candidate and the text of a card.

    Caption and alt text count double: a publisher's caption names what the
    figure IS, whereas page context merely names what was nearby, and "nearby"
    on a dense page means half a chapter.
    """
    query = _tokens(query_text)
    if not query:
        return 0
    strong = _tokens(" ".join(str(image.get(k) or "") for k in ("caption", "alt")))
    weak = _tokens(" ".join(str(image.get(k) or "") for k in ("chapter", "context")))
    return 2 * len(strong & query) + len(weak & query)


def shortlist(
    images: Optional[List[Dict[str, Any]]],
    query_text: str,
    *,
    max_position: Optional[float] = None,
    limit: int = MAX_SHORTLIST,
) -> List[Dict[str, Any]]:
    """The candidates a model may choose from for this request.

    `max_position` is the Story guard: 0..1, the furthest through the book the
    reader has got. Applied HERE, before the model sees anything, so a spoiler
    is not merely rejected later — it is never offered. (It is checked again
    after the model answers; this is the cheap half of a belt-and-braces pair.)
    """
    if not images or not isinstance(images, (list, tuple)):
        return []
    scored = []
    for img in images:
        if not isinstance(img, dict) or not img.get("id"):
            continue
        if max_position is not None:
            # Same unit rule as validate_selection: an incomparable position is
            # refused, never converted.
            if img.get("position_basis") != "words":
                continue
            pos = img.get("position")
            if not isinstance(pos, (int, float)) or pos > max_position:
                continue
        score = relevance(img, query_text)
        if score < MIN_RELEVANCE:
            continue
        order = img.get("order")
        scored.append((score, order if isinstance(order, int) else 0, img))
    # Best score first; source order breaks ties so the result is stable.
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [img for _score, _order, img in scored[:limit]]


def safe_shortlist(images, query_text: str, **kwargs) -> List[Dict[str, Any]]:
    """`shortlist` that cannot fail a session. Returns [] on any error.

    The image rows are JSON from the database, so a row written by an older
    version, a partially-migrated book or a corrupted blob can be any shape at
    all. None of that is worth a failed nibble: the text half of a session is
    complete and correct without a single picture, so every entry point from
    session_service goes through here.
    """
    try:
        return shortlist(images, query_text, **kwargs)
    except Exception as e:  # noqa: BLE001 — a garnish must never break the meal
        logger.warning("image shortlist failed (%s) — text-only session", type(e).__name__)
        return []


def safe_prompt(candidates: List[Dict[str, Any]]) -> str:
    """`describe_for_prompt` that cannot fail a session."""
    try:
        return describe_for_prompt(candidates)
    except Exception as e:  # noqa: BLE001
        logger.warning("image prompt build failed (%s) — text-only session", type(e).__name__)
        return ""


def prompt_rows(candidates: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """What the model is allowed to see: an opaque id and a little description.

    No key, no URL, no filename, no page number it could turn into one. If a
    field here ever grows into something addressable, the model gains the
    ability to ask for an arbitrary object, and the server-side check stops
    being the only gate.
    """
    rows = []
    for c in candidates:
        desc = " — ".join(x for x in (c.get("caption"), c.get("alt")) if x)
        if not desc:
            desc = (c.get("context") or "")[:MAX_DESC_CHARS]
        rows.append({
            "id": str(c["id"]),
            "description": (desc or "an illustration from the book")[:MAX_DESC_CHARS],
        })
    return rows


def describe_for_prompt(candidates: List[Dict[str, Any]]) -> str:
    """The shortlist as prompt text, or "" when there is nothing to offer."""
    rows = prompt_rows(candidates)
    if not rows:
        return ""
    lines = "\n".join("- %s: %s" % (r["id"], r["description"]) for r in rows)
    return (
        "AVAILABLE BOOK IMAGES (optional). You may set a card's \"imageId\" to ONE "
        "of these ids, and only when the picture genuinely illustrates that card's "
        "idea. Most cards should have no image — leave \"imageId\" null rather than "
        "reaching for a loose match. Never invent an id, a filename or a URL.\n"
        + lines
    )


def card_text(card: Dict[str, Any]) -> str:
    """The words of one card, for judging whether a picture belongs on IT."""
    return " ".join(str(card.get(k) or "") for k in ("title", "body", "highlight"))


def validate_selection(
    selected_id: Optional[str],
    *,
    shortlisted: List[Dict[str, Any]],
    user_id: str,
    item_id: str,
    max_position: Optional[float] = None,
    card: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Re-check the model's choice from the stored rows. None means no image.

    Every condition is verified again even though the shortlist was built from
    the same data a moment ago, because the value being checked came back from
    a model in between. Returning None is always acceptable — it yields a
    text-only card, which is the expected outcome for most cards.

    `card` enables the check that shortlist membership alone cannot make. The
    shortlist is built once per DECK, from all of its retrieved passages, so
    "this candidate is relevant to this session" is the only thing membership
    proves. A habit-loop diagram legitimately shortlisted for a deck was
    therefore accepted on that deck's unrelated marine-biology card. Relevance
    is per card, so it is checked per card.
    """
    if not selected_id or not isinstance(selected_id, str):
        return None

    # Must be one we actually offered. This single check also defeats an
    # invented id, another book's id and another user's id — but the explicit
    # ownership checks below stay, because a future caller might build the
    # shortlist differently and this must not silently become the only gate.
    match = next((c for c in shortlisted if str(c.get("id")) == selected_id), None)
    if match is None:
        logger.info("image selection rejected: %r was not in the shortlist", selected_id[:40])
        return None

    if str(match.get("user_id") or "") != str(user_id):
        logger.warning("image selection rejected: candidate owner mismatch")
        return None
    if str(match.get("item_id") or "") != str(item_id):
        logger.warning("image selection rejected: candidate belongs to another book")
        return None
    if not match.get("key"):
        return None

    if max_position is not None:
        pos = match.get("position")
        # Positions measured in pages or spine documents are NOT comparable
        # with story progress, which is a word offset — a figure on page 12 of
        # 300 is not "4% through" when the first 40 pages are front matter.
        # An incomparable unit is refused rather than converted, because a
        # wrong conversion here shows a reader a scene they have not reached.
        if match.get("position_basis") != "words":
            logger.info("image selection rejected: position basis %r is not comparable "
                        "with story progress", match.get("position_basis"))
            return None
        if pos is None or pos > max_position:
            logger.info("image selection rejected: candidate is ahead of the reader")
            return None

    if card is not None and relevance(match, card_text(card)) < MIN_RELEVANCE:
        logger.info("image selection rejected: not relevant to this particular card")
        return None

    return match


def card_image_payload(candidate: Dict[str, Any]) -> Dict[str, Any]:
    """The image metadata persisted on a card and sent to the app.

    Carries a durable reference — the candidate id, resolved through an
    authenticated endpoint — and NOT a presigned URL. Presigned URLs expire in
    an hour; a nibble is replayed from the Nibble Bank months later, and a
    persisted expiring URL would be a card that works today and 404s in
    August. The raw S3 key is deliberately absent too: the client has no use
    for it and it is not the client's to know.
    """
    return {
        "source": "book",
        "id": candidate["id"],
        # Book-scoped. A candidate id salted only by checksum collided across
        # books — the same figure uploaded twice produced one id and delivery
        # had two rows to choose from. The path now names both, so the endpoint
        # resolves exactly one object.
        "url": "/library/%s/images/%s" % (candidate.get("item_id", ""), candidate["id"]),
        # The client builds the same path from these two, so a card persisted
        # before the path changed still resolves.
        "itemId": candidate.get("item_id"),
        "alt": candidate.get("alt") or candidate.get("caption") or "",
        # Cropping the axis off a chart destroys it; letterboxing a photograph
        # merely looks a little plain.
        "fit": "contain" if candidate.get("visual") == "diagram" else "cover",
        "visual": candidate.get("visual") or "photo",
        "w": candidate.get("w"),
        "h": candidate.get("h"),
    }


def attach_images(
    cards: List[Dict[str, Any]],
    *,
    shortlisted: List[Dict[str, Any]],
    user_id: str,
    item_id: str,
    max_position: Optional[float] = None,
) -> int:
    """Replace each card's model-supplied `imageId` with validated metadata.

    Mutates `cards` in place and returns how many images survived validation.
    The `imageId` key is always removed, valid or not: it is an internal
    protocol between the server and the model, and letting it reach the client
    would leak the shortlist's vocabulary into the app's data model.

    One picture may not appear on two cards in the same session — a repeated
    figure reads as a bug, and the model has no way to know it already used it.
    """
    used = set()
    attached = 0
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        selected = card.pop("imageId", None)
        if selected in used:
            continue
        candidate = validate_selection(
            selected, shortlisted=shortlisted, user_id=user_id,
            item_id=item_id, max_position=max_position,
            # Relevance to THIS card, not merely to the deck.
            card=card,
        )
        if candidate is None:
            continue
        card["image"] = card_image_payload(candidate)
        used.add(candidate["id"])
        attached += 1
    return attached
