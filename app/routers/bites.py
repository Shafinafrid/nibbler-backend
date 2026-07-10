from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import date, datetime
from typing import Optional, List
from pydantic import BaseModel
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.bite import DailyBite, SavedBite
from app.models.library import LibraryItem
from app.models.streak import Streak
from app.schemas.bite import BiteResponse, SavedBiteResponse, BiteHistoryResponse, StreakResponse
from app.services.bite_generator import BiteGenerator
from app.services.claude import ClaudeService
from app.services.embedding_service import EmbeddingService
from app.services import mixpanel_service
import uuid

router = APIRouter(prefix="/bites", tags=["bites"])

# ── Per-book session generation (July 2026) ───────────────────────────────

# read length → total cards in the deck / retrieval breadth / story words
CARD_TARGETS = {5: 5, 10: 8, 15: 12}
WISDOM_TOP_K = {5: 6, 10: 10, 15: 14}
STORY_WORDS = {5: 1100, 10: 2200, 15: 3300}


class SessionProfile(BaseModel):
    name: Optional[str] = None
    lifeArea: Optional[str] = None
    aspirationLabel: Optional[str] = None
    aspirationUnderstanding: Optional[str] = None
    confidenceStyle: Optional[str] = None
    goalOrientation: Optional[str] = None
    contentMode: Optional[str] = None
    interests: Optional[List[str]] = None


class SessionRequest(BaseModel):
    library_item_id: str
    read_length: int = 5
    growth_profile: Optional[SessionProfile] = None
    force_new: bool = False


class SessionResponse(BaseModel):
    id: str
    library_item_id: str
    date: date
    mode: str
    read_length: int
    title: str
    chapter: Optional[str] = None
    headline: Optional[str] = None
    preview: Optional[str] = None
    cards: list
    quiz: Optional[list] = None
    story_finished: bool = False


def _bite_to_session(bite: DailyBite) -> SessionResponse:
    return SessionResponse(
        id=bite.id,
        library_item_id=bite.library_item_id,
        date=bite.date,
        mode=bite.mode or "wisdom",
        read_length=bite.read_length or 5,
        title=bite.title,
        chapter=bite.chapter,
        headline=bite.headline,
        preview=bite.preview,
        cards=bite.cards or [],
        quiz=bite.quiz,
        story_finished=bool(bite.theme == "story_finished"),
    )


@router.post("/session", response_model=SessionResponse)
async def get_or_create_session(
    data: SessionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Today's card-deck session for one library item. Cached per (user, item, day)."""
    item = db.query(LibraryItem).filter(
        LibraryItem.id == data.library_item_id,
        LibraryItem.user_id == current_user.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found.")
    if not item.processed:
        raise HTTPException(status_code=409, detail="Nibbler is still reading this one — try again in a moment.")

    read_length = data.read_length if data.read_length in CARD_TARGETS else 5
    today = date.today()

    existing = db.query(DailyBite).filter(
        DailyBite.user_id == current_user.id,
        DailyBite.library_item_id == item.id,
        DailyBite.date == today,
    ).first()
    if existing and existing.cards and not data.force_new:
        return _bite_to_session(existing)
    if existing and data.force_new:
        db.delete(existing)
        db.commit()

    claude = ClaudeService(is_premium=current_user.effective_premium)
    mode = item.mode or "wisdom"
    card_target = CARD_TARGETS[read_length]
    story_finished = False

    if mode == "story":
        words = (item.content or "").split()
        if not words:
            raise HTTPException(status_code=422, detail="No readable text stored for this book.")
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
            excerpt = " ".join(words[progress:progress + n])
            part_number = progress // n + 1
            try:
                result = await claude.generate_story_session(
                    book_title=item.title, author=item.author,
                    excerpt=excerpt, card_target=max(3, card_target - 1),
                    part_number=part_number,
                )
            except Exception as e:
                raise HTTPException(status_code=502, detail=f"Session generation failed: {e}")
            item.story_progress = min(progress + n, len(words))
    else:
        profile = (data.growth_profile.model_dump() if data.growth_profile else {}) or {}
        query_bits = [
            profile.get("aspirationUnderstanding") or profile.get("aspirationLabel") or "",
            " ".join(profile.get("interests") or []),
            profile.get("lifeArea") or "",
        ]
        query = " ".join(b for b in query_bits if b).strip() or item.title
        embeddings = EmbeddingService()
        chunks = await embeddings.search_item(
            query=query, user_id=current_user.id, item_id=item.id,
            top_k=WISDOM_TOP_K[read_length],
        )
        if not chunks and item.content:
            # Pinecone unavailable — fall back to the beginning of the stored text
            chunks = [item.content[:8000]]
        if not chunks:
            raise HTTPException(status_code=422, detail="No indexed content found for this item.")
        try:
            result = await claude.generate_wisdom_session(
                book_title=item.title, author=item.author,
                profile=profile, context_chunks=chunks,
                card_target=card_target, read_length=read_length,
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"Session generation failed: {e}")

    bite = DailyBite(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        library_item_id=item.id,
        date=today,
        title=(result.get("title") or item.title)[:250],
        insight=result.get("preview") or result.get("headline") or "",
        reflection="",
        action="",
        source=item.title,
        theme="story_finished" if story_finished else (mode),
        cards=result.get("cards") or [],
        quiz=result.get("quiz"),
        read_length=read_length,
        mode=mode,
        chapter=(result.get("chapter") or "")[:250],
        headline=(result.get("headline") or "")[:500],
        preview=result.get("preview") or "",
    )
    db.add(bite)
    db.commit()
    db.refresh(bite)

    await mixpanel_service.track("session_generated", current_user.id, {
        "mode": mode, "read_length": read_length, "cards": len(bite.cards or []),
    })
    return _bite_to_session(bite)


def _bite_to_response(bite: DailyBite, saved_ids: set) -> BiteResponse:
    return BiteResponse(
        id=bite.id,
        title=bite.title,
        insight=bite.insight,
        reflection=bite.reflection,
        action=bite.action,
        source=bite.source,
        theme=bite.theme,
        date=bite.date,
        is_saved=bite.id in saved_ids,
    )


@router.get("/today", response_model=BiteResponse)
async def get_todays_bite(
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get today's daily bite. Generates one if it doesn't exist yet."""
    today = date.today()

    bite = db.query(DailyBite).filter(
        DailyBite.user_id == current_user.id,
        DailyBite.date == today,
    ).first()

    if not bite:
        if not current_user.profile:
            raise HTTPException(status_code=400, detail="Complete onboarding before getting your daily bite.")

        generator = BiteGenerator(is_premium=current_user.effective_premium)
        bite_data = await generator.generate(
            profile=current_user.profile,
            user_id=current_user.id,
        )

        bite = DailyBite(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            date=today,
            **bite_data,
        )
        db.add(bite)

        # Update streak + track analytics in background
        background_tasks.add_task(update_streak, current_user.id)
        background_tasks.add_task(
            mixpanel_service.track,
            "bite_generated",
            current_user.id,
            {"theme": bite_data.get("theme"), "is_premium": current_user.effective_premium},
        )
        db.commit()
        db.refresh(bite)

    saved_ids = {s.bite_id for s in db.query(SavedBite).filter(SavedBite.user_id == current_user.id).all()}
    return _bite_to_response(bite, saved_ids)


@router.get("/history", response_model=BiteHistoryResponse)
async def get_bite_history(
    limit: int = 30,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get past daily bites. Free users: last 7 days. Premium: full archive."""
    query = db.query(DailyBite).filter(DailyBite.user_id == current_user.id)

    if not current_user.effective_premium:
        from datetime import timedelta
        cutoff = date.today() - timedelta(days=7)
        query = query.filter(DailyBite.date >= cutoff)

    bites = query.order_by(DailyBite.date.desc()).limit(limit).all()
    saved_ids = {s.bite_id for s in db.query(SavedBite).filter(SavedBite.user_id == current_user.id).all()}

    return BiteHistoryResponse(
        bites=[_bite_to_response(b, saved_ids) for b in bites],
        total=len(bites),
    )


@router.post("/{bite_id}/save", response_model=dict)
async def save_bite(
    bite_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    bite = db.query(DailyBite).filter(
        DailyBite.id == bite_id,
        DailyBite.user_id == current_user.id,
    ).first()
    if not bite:
        raise HTTPException(status_code=404, detail="Bite not found.")

    existing = db.query(SavedBite).filter(
        SavedBite.bite_id == bite_id,
        SavedBite.user_id == current_user.id,
    ).first()
    if existing:
        return {"message": "Already saved"}

    saved = SavedBite(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        bite_id=bite_id,
    )
    db.add(saved)
    db.commit()
    return {"message": "Saved"}


@router.delete("/{bite_id}/save")
async def unsave_bite(
    bite_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    saved = db.query(SavedBite).filter(
        SavedBite.bite_id == bite_id,
        SavedBite.user_id == current_user.id,
    ).first()
    if not saved:
        raise HTTPException(status_code=404, detail="Saved bite not found.")

    db.delete(saved)
    db.commit()
    return {"message": "Removed from saved"}


@router.get("/saved", response_model=list[SavedBiteResponse])
async def get_saved_bites(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    saved = db.query(SavedBite).filter(
        SavedBite.user_id == current_user.id,
    ).order_by(SavedBite.saved_at.desc()).all()

    saved_ids = {s.bite_id for s in saved}
    return [
        SavedBiteResponse(
            id=s.id,
            bite=_bite_to_response(s.bite, saved_ids),
            saved_at=s.saved_at,
        )
        for s in saved
    ]


async def update_streak(user_id: str):
    """Background task to update the user's streak."""
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        today = date.today()
        streak = db.query(Streak).filter(Streak.user_id == user_id).first()

        if not streak:
            streak = Streak(
                id=str(uuid.uuid4()),
                user_id=user_id,
                current_streak=1,
                longest_streak=1,
                last_active_date=today,
                total_bites_read=1,
            )
            db.add(streak)
        else:
            if streak.last_active_date == today:
                return  # Already checked in today

            from datetime import timedelta
            yesterday = today - timedelta(days=1)
            if streak.last_active_date == yesterday:
                streak.current_streak += 1
            else:
                streak.current_streak = 1

            streak.longest_streak = max(streak.current_streak, streak.longest_streak)
            streak.last_active_date = today
            streak.total_bites_read += 1

        db.commit()
    finally:
        db.close()
