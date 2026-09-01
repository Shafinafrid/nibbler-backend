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
#
# NOTE: "personalize" is deliberately NOT here. That card kind exists and the
# app renders it, but it is built and inserted SERVER-SIDE after generation
# (session_service._insert_personalization_card) and must never be emitted by
# a model: allowing it here would let a provider invent its own preference
# question, with options carrying tags that mutate the user's growth profile,
# bypassing the grounding validation the real one goes through.
CARD_KINDS = ["hook", "insight", "quiz", "prompt", "summary"]

# Fixed vocabulary a personalization option's `tag` must come from. Closed on
# purpose — the SAME reasoning as aspiration_schema()'s contentMode/
# motivation/goalOrientation enums below: a free-form string, or worse, a
# model-invented NUMERIC delta per option, would make identical answer text
# shift the growth profile differently depending on which provider (Luna,
# Haiku, Qwen) generated it. The model only ever picks from this closed set;
# what each tag actually DOES to a profile is a fixed, code-owned mapping
# (PERSONALIZATION_TAG_DELTAS, applied client-side via profileEvents.js —
# see app/routers/bites.py's personalize-answer endpoint), never something
# the model decides the magnitude of.
PERSONALIZATION_TAGS = [
    "prefers_automation", "prefers_manual_control",
    "prefers_analytical_depth", "prefers_simplicity",
    "increase_confidence", "decrease_confidence",
    "shift_practical", "shift_reflective", "shift_analytical",
]


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


def personalization_option_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"text": _str(), "tag": {"type": "string", "enum": PERSONALIZATION_TAGS}},
        "required": ["text", "tag"],
    }


def personalization_schema() -> Dict[str, Any]:
    """The standalone personalization-question call's own schema — separate
    from wisdom_schema/card_schema because this call runs BEFORE and
    INDEPENDENTLY of the deck-generation call (see session_service's
    two-call design: the question is generated and grounding-validated on
    its own, then handed to the deck prompt to reproduce verbatim)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "question": _str(),
            "eyebrow": _str(),
            "options": {
                "type": "array",
                "items": personalization_option_schema(),
                "minItems": 2,
                "maxItems": 4,
            },
            "highlight": _str(nullable=True),
        },
        "required": ["question", "eyebrow", "options", "highlight"],
    }


def personalization_interpret_schema() -> Dict[str, Any]:
    """The free-text "something else" interpretation call's schema — maps
    the user's own words onto the SAME closed PERSONALIZATION_TAGS
    vocabulary the multiple-choice options use, never a free-form delta."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": PERSONALIZATION_TAGS},
                "minItems": 0,
                "maxItems": 3,
            },
            "summary": _str(),
        },
        "required": ["tags", "summary"],
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
        # NOTE: there is deliberately no `personalizeOptions` here. A
        # personalize card is never produced by the deck model — it is built
        # and inserted server-side by session_service after generation (see
        # _insert_personalization_card). Declaring the field would oblige
        # every card the model returns to carry it (the strict subset has no
        # optional keys), for a card kind the model must never emit.
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
