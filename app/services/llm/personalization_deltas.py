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
