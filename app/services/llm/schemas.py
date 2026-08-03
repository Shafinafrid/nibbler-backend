"""
One JSON Schema per workflow, shared by all three providers.

These are written to OpenAI's *strict* Structured Outputs subset, which is the
narrowest of the three, so the same document can be handed to Luna's
`text.format`, Anthropic's tool `input_schema`, and llama.cpp's
`response_format.json_schema` without per-provider variants. Two rules of that
subset shape everything below:

  * EVERY property must appear in `required`, and `additionalProperties` must
    be false. There is no such thing as an optional key.
  * Optionality is therefore expressed as a NULLABLE TYPE — `["string","null"]`
    — not by omitting the key. A quiz card's `body` is null; an insight card's
    `options` is null.

That last point is why `card_schema()` is one permissive shape rather than an
`anyOf` over five card kinds. A discriminated union is expressible, but it
multiplies the grammar the model must satisfy for no gain: the per-kind rules
("a quiz card has exactly four options, exactly one correct") are enforced in
`validation.py`, which can also check the things a schema genuinely cannot —
card ORDER, one-correct-answer, and heading counts matching card counts.

Array cardinality IS enforced here: `minItems`/`maxItems` are part of the
strict subset, so "exactly CARD_TARGET cards" is a schema constraint, not a
hope. See validation.py for what the schema still cannot say.
"""

from typing import Any, Dict

# Card kinds the mobile app knows how to render. A deck containing anything
# else would render as a blank card, so the enum is a real safety constraint,
# not documentation.
CARD_KINDS = ["hook", "insight", "quiz", "prompt", "summary"]


def _str(nullable: bool = False) -> Dict[str, Any]:
    return {"type": ["string", "null"] if nullable else "string"}


def quiz_option_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"text": _str(), "correct": {"type": "boolean"}},
        "required": ["text", "correct"],
    }


def quiz_question_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question": _str(),
            "options": {
                "type": "array",
                "items": quiz_option_schema(),
                "minItems": 4,
                "maxItems": 4,
            },
            "explanation": _str(nullable=True),
        },
        "required": ["question", "options", "explanation"],
    }


def card_schema(with_images: bool = False) -> Dict[str, Any]:
    """One card. Fields not used by a given kind are null, per the strict
    subset's no-optional-keys rule — see the module docstring.

    `with_images` adds `imageId`, present only when the book actually has
    candidate figures to offer. Leaving it out otherwise is not an
    optimisation: a nullable field the model cannot legitimately fill is an
    invitation to invent one.
    """
    props: Dict[str, Any] = {
        "kind": {"type": "string", "enum": CARD_KINDS},
        "eyebrow": _str(),
        "title": _str(),
        "body": _str(nullable=True),
        # Insight cards only: an exact pull-quote from the source.
        "highlight": _str(nullable=True),
        # Quiz cards only.
        "options": {
            "type": ["array", "null"],
            "items": quiz_option_schema(),
            "minItems": 4,
            "maxItems": 4,
        },
        "explanation": _str(nullable=True),
    }
    required = ["kind", "eyebrow", "title", "body", "highlight", "options", "explanation"]
    if with_images:
        # An id from the supplied shortlist, or null. NOT a URL, a filename or
        # a path — the server re-checks the value against the shortlist and
        # rejects anything it did not itself offer, so this can only ever be a
        # choice among what we handed over.
        props["imageId"] = _str(nullable=True)
        required.append("imageId")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": props,
        "required": required,
    }


def wisdom_schema(card_target: int, quiz_target: int,
                  with_images: bool = False) -> Dict[str, Any]:
    """A full Wisdom deck. Cardinality is baked in per request: the deck is
    exactly `card_target` cards and the review quiz exactly `quiz_target`
    questions, so a short deck is rejected by the provider rather than reaching
    a user with three cards where five were promised."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "title": _str(),
            "chapter": _str(),
            "headline": _str(),
            "preview": _str(),
            "cards": {
                "type": "array",
                "items": card_schema(with_images),
                "minItems": card_target,
                "maxItems": card_target,
            },
            "quiz": {
                "type": "array",
                "items": quiz_question_schema(),
                "minItems": quiz_target,
                "maxItems": quiz_target,
            },
        },
        "required": ["title", "chapter", "headline", "preview", "cards", "quiz"],
    }


def story_schema(card_count: int, with_images: bool = False) -> Dict[str, Any]:
    """Titling for a Story portion. The prose is NOT here and must never be:
    story cards carry the author's own text, cut server-side, and the model's
    only job is to name each card.

    With images, it may also associate one candidate id per card — an array
    positionally parallel to `headings`, because the cards themselves are
    server-owned and have no other identifier the model could refer to.
    """
    props: Dict[str, Any] = {
        "title": _str(),
        "headline": _str(),
        "preview": _str(),
        "headings": {
            "type": "array",
            "items": _str(),
            "minItems": card_count,
            "maxItems": card_count,
        },
    }
    required = ["title", "headline", "preview", "headings"]
    if with_images:
        props["imageIds"] = {
            "type": "array",
            "items": _str(nullable=True),
            "minItems": card_count,
            "maxItems": card_count,
        }
        required.append("imageIds")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": props,
        "required": required,
    }


def aspiration_schema() -> Dict[str, Any]:
    """The onboarding growth-profile seed.

    The enums matter more than they look: `contentMode` picks which interaction
    card a Wisdom deck ends on, and `lifeArea` feeds retrieval. A free-form
    string here would silently degrade both.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "needsClarification": {"type": "boolean"},
            "clarifyPrompt": _str(nullable=True),
            "lifeArea": _str(),
            "contentMode": {"type": "string", "enum": ["analytical", "reflective", "practical"]},
            "motivation": {"type": "string", "enum": ["career", "skill", "habit", "curiosity", "prep"]},
            "motivationType": {"type": "string", "enum": ["intrinsic", "instrumental", "mixed"]},
            "goalOrientation": {"type": "string", "enum": ["mastery", "summary", "application"]},
            "interests": {"type": "array", "items": _str(), "minItems": 1, "maxItems": 4},
            "profileName": _str(),
            "confirmation": _str(),
            "understanding": _str(),
        },
        "required": [
            "needsClarification", "clarifyPrompt", "lifeArea", "contentMode",
            "motivation", "motivationType", "goalOrientation", "interests",
            "profileName", "confirmation", "understanding",
        ],
    }
