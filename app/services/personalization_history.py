"""Server-owned question memory, shared across every book serving one goal.

Do not ask the same preference with different wording. Answered dimensions stay
retired; unanswered cards reserve a dimension only temporarily, so skipping a
question does not permanently stop the profile from learning.
"""
from difflib import SequenceMatcher
from datetime import datetime, timedelta
import re

DIMENSIONS = (
    ("prefers_automation", "prefers_manual_control"),
    ("prefers_analytical_depth", "prefers_simplicity"),
    ("increase_confidence", "decrease_confidence"),
    ("shift_practical", "shift_reflective", "shift_analytical"),
    ("prefers_small_steps", "prefers_bold_experiments"),
    ("prefers_solo_progress", "prefers_accountability"),
    ("prefers_routine", "prefers_flexibility"),
    ("prefers_examples", "prefers_principles"),
    ("prefers_fast_feedback", "prefers_long_horizon"),
    ("prefers_visual_learning", "prefers_verbal_learning"),
)

PENDING_DIMENSION_TTL = timedelta(days=7)


def question_memory(db, user_id, profile_id):
    from app.models.personalization import PersonalizationQuestion
    if not profile_id:
        return []
    # Include pending questions: scheduled sessions must not repeat a question
    # just because its first answer has not synced yet. Never trust a client's
    # truncated personalizationHistory cache or mix another user's/profile's rows.
    rows = db.query(PersonalizationQuestion).filter_by(
        user_id=user_id, profile_id=profile_id,
    ).populate_existing().order_by(PersonalizationQuestion.created_at.desc()).all()
    return [{
        "question": row.question,
        "options": row.options if isinstance(row.options, list) else [],
        "answer": row.answer_free_text or next((
            o.get("text", "") for o in (row.options if isinstance(row.options, list) else [])
            if isinstance(o, dict) and o.get("id") == row.answer_option_id
        ), ""),
        "tags": row.applied_tags or [],
        "status": row.status,
        "created_at": row.created_at,
    } for row in rows]


def persist_novel_question(db, *, user_id, profile_id, bite_id, item_id, question, chunk_ids):
    """Serialize the final novelty check + insert, not the slow generation.

    Two books for one goal can generate concurrently. The later writer drops
    its optional card if the first writer has already asked that dimension.
    The deck's lease has already been finalized before entering here.
    """
    from app.models.personalization import PersonalizationQuestion
    from app.models.bite import DailyBite
    from app.services.profile_resolution import lock_user_scope
    lock_user_scope(db, user_id)
    if not is_novel_question(question, question_memory(db, user_id, profile_id)):
        bite = db.query(DailyBite).filter_by(id=bite_id, user_id=user_id).populate_existing().first()
        if bite:
            bite.cards = [c for c in bite.cards or [] if c.get("kind") != "personalize"]
        db.commit()
        return False
    db.add(PersonalizationQuestion(
        user_id=user_id, profile_id=profile_id, daily_bite_id=bite_id,
        library_item_id=item_id, question=question["question"],
        options=question["options"], source_chunk_ids=chunk_ids,
    ))
    db.commit()
    return True


def available_tags(history):
    seen = set()
    for row in history:
        created = row.get("created_at")
        if (
            row.get("status") in {"pending", "processing"}
            and isinstance(created, datetime)
            and created < datetime.utcnow() - PENDING_DIMENSION_TTL
        ):
            # Still keep the old wording in history for similarity rejection,
            # but release its dimension: the user never answered it.
            continue
        seen.update(row.get("tags") or [])
        seen.update(o.get("tag") for o in row.get("options", []) if isinstance(o, dict))
    return [tag for dimension in DIMENSIONS if not seen.intersection(dimension)
            for tag in dimension]


def is_novel_question(question, history):
    allowed = set(available_tags(history))
    options = question.get("options") or []
    if not options or any(o.get("tag") not in allowed for o in options):
        return False
    normalize = lambda s: " ".join(re.findall(r"\w+", (s or "").casefold()))
    text = normalize(question.get("question"))
    return bool(text) and all(
        SequenceMatcher(None, text, normalize(row.get("question"))).ratio() < 0.78
        for row in history
    )
