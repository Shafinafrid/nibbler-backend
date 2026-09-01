"""
Fixed tag → growth-profile-delta mapping for the dynamic personalization
feature (Aug 2026).

This mapping is NOT applied by the backend itself — the backend only ever
returns tags (see app/routers/bites.py's personalize-answer endpoint). The
app applies the deltas locally via profileEvents.js's `personalization_answered`
case, reusing the SAME clamp/bump vocabulary every other profile event
already uses (bumpInterest, updateDifficulty, clamp 0-1).

This Python copy exists as the single documented reference for what each
tag means — kept in manual sync with profileEvents.js's JS mirror, the same
way CARD_TARGETS/QUIZ_TARGETS are manually kept in sync between
session_service.py and prompts.py elsewhere in this codebase (no shared-
schema codegen exists here). `label` is the short, human-readable phrase
shown on the app's growth-timeline screen (e.g. "-> leaning toward automated
approaches") — a fixed lookup, never model-generated text, so it costs
nothing extra to render.

See app/services/llm/schemas.py's PERSONALIZATION_TAGS for the closed
vocabulary a model may choose from, and that module's docstring for why the
mapping itself is fixed/deterministic rather than LLM-emitted.
"""

# Tag pairs that cannot both be true of one answer. Round-5: sanitation runs
# on every REPLAY path, not only when a new answer is recorded — rows
# answered before this rule existed still hold contradictory pairs, and
# returning them raw lets a client apply both halves (a +0.05 and a -0.05
# that cancel, or a contentMode assigned twice with the last write winning).
OPPOSING_TAG_PAIRS = [
    ("increase_confidence", "decrease_confidence"),
    ("prefers_automation", "prefers_manual_control"),
    ("prefers_analytical_depth", "prefers_simplicity"),
    ("shift_practical", "shift_reflective"),
    ("shift_practical", "shift_analytical"),
    ("shift_reflective", "shift_analytical"),
]


def sanitize_tags(tags):
    """Order-preserving dedupe, then drop BOTH halves of any opposing pair.

    Dropping both rather than keeping the first is deliberate: an answer
    claiming a user wants both more and less confidence expresses no
    preference, and picking a half would invent one.

    Applied to newly-resolved answers AND to historical `applied_tags` on
    every path that returns them.
    """
    clean = list(dict.fromkeys(t for t in (tags or []) if t))
    conflicted = set()
    for a, b in OPPOSING_TAG_PAIRS:
        if a in clean and b in clean:
            conflicted.add(a)
            conflicted.add(b)
    return [t for t in clean if t not in conflicted]


PERSONALIZATION_TAG_DELTAS = {
    "prefers_automation": {
        "interestBump": ("automation", 0.15),
        "contentModeShift": "practical",
        "label": "leaning toward automated, hands-off approaches",
    },
    "prefers_manual_control": {
        "interestBump": ("hands_on_practice", 0.15),
        "contentModeShift": "analytical",
        "label": "leaning toward hands-on, deep-dive approaches",
    },
    "prefers_analytical_depth": {
        "interestBump": ("deep_analysis", 0.15),
        "contentModeShift": "analytical",
        "label": "leaning toward analytical depth",
    },
    "prefers_simplicity": {
        "interestBump": ("simple_frameworks", 0.15),
        "contentModeShift": "practical",
        "label": "leaning toward simple, practical frameworks",
    },
    "increase_confidence": {
        "selfEfficacyDelta": 0.05,
        "label": "growing more confident in this area",
    },
    "decrease_confidence": {
        "selfEfficacyDelta": -0.05,
        "label": "wanting a gentler pace in this area",
    },
    "shift_practical": {
        "contentModeShift": "practical",
        "label": "leaning toward practical application",
    },
    "shift_reflective": {
        "contentModeShift": "reflective",
        "label": "leaning toward reflection and meaning",
    },
    "shift_analytical": {
        "contentModeShift": "analytical",
        "label": "leaning toward understanding how things work",
    },
}
