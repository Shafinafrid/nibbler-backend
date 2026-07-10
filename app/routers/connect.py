"""
Connect — chat with your own books (premium).

  POST /connect/insights  → per-book analytics for the Connect tab:
                            goal-relevance score (from vector similarity between
                            the user's growth profile and the book's chunks) and
                            the passages that speak most to their goal.
  POST /connect/chat      → grounded chat: Claude answers ONLY from excerpts of
                            this book (retrieved per question), and says so when
                            the answer isn't in the book.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional, List
from pydantic import BaseModel
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem
from app.services.claude import ClaudeService
from app.services.embedding_service import EmbeddingService
from app.services import mixpanel_service

router = APIRouter(prefix="/connect", tags=["connect"])


class ConnectProfile(BaseModel):
    name: Optional[str] = None
    lifeArea: Optional[str] = None
    aspirationLabel: Optional[str] = None
    aspirationUnderstanding: Optional[str] = None
    interests: Optional[List[str]] = None


class InsightsRequest(BaseModel):
    library_item_id: str
    growth_profile: Optional[ConnectProfile] = None


class InsightsResponse(BaseModel):
    relevance_pct: int
    relevance_band: str
    top_passages: List[str]
    chunk_count: int
    mode: str


class ChatRequest(BaseModel):
    library_item_id: str
    message: str
    history: List[dict] = []


class ChatResponse(BaseModel):
    reply: str


def _get_item(item_id: str, user: User, db: Session) -> LibraryItem:
    item = db.query(LibraryItem).filter(
        LibraryItem.id == item_id,
        LibraryItem.user_id == user.id,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="Library item not found.")
    if not item.processed:
        raise HTTPException(status_code=409, detail="Nibbler is still reading this one.")
    return item


@router.post("/insights", response_model=InsightsResponse)
async def get_insights(
    data: InsightsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = _get_item(data.library_item_id, current_user, db)

    profile = data.growth_profile
    query_bits = []
    if profile:
        query_bits = [
            profile.aspirationUnderstanding or profile.aspirationLabel or "",
            " ".join(profile.interests or []),
            profile.lifeArea or "",
        ]
    query = " ".join(b for b in query_bits if b).strip() or "personal growth and learning"

    embeddings = EmbeddingService()
    scored = await embeddings.search_item_scored(
        query=query, user_id=current_user.id, item_id=item.id, top_k=8,
    )

    if not scored:
        return InsightsResponse(
            relevance_pct=0, relevance_band="Unknown",
            top_passages=[], chunk_count=item.chunk_count or 0,
            mode=item.mode or "wisdom",
        )

    # Voyage cosine similarity for on-topic passages typically lands ~0.55–0.75;
    # unrelated content sits ~0.30–0.45. Map the top-5 average onto 0–100.
    top5 = [s for _, s in scored[:5]]
    avg = sum(top5) / len(top5)
    pct = round((avg - 0.35) / (0.75 - 0.35) * 100)
    pct = max(4, min(97, pct))
    band = "Strong match" if pct >= 65 else "Good match" if pct >= 40 else "Side quest"

    # The passages that speak most to their goal (trimmed for card display)
    passages = []
    for text, _ in scored[:3]:
        t = " ".join(text.split())
        passages.append(t[:220] + ("…" if len(t) > 220 else ""))

    return InsightsResponse(
        relevance_pct=pct,
        relevance_band=band,
        top_passages=passages,
        chunk_count=item.chunk_count or 0,
        mode=item.mode or "wisdom",
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(
    data: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    message = (data.message or "").strip()
    if not message:
        raise HTTPException(status_code=422, detail="Message is empty.")

    item = _get_item(data.library_item_id, current_user, db)

    embeddings = EmbeddingService()
    excerpts = await embeddings.search_item(
        query=message, user_id=current_user.id, item_id=item.id, top_k=8,
    )
    if not excerpts and item.content:
        excerpts = [item.content[:8000]]
    if not excerpts:
        raise HTTPException(status_code=422, detail="No indexed content found for this book.")

    claude = ClaudeService(is_premium=current_user.effective_premium)
    try:
        reply = await claude.chat_with_book(
            book_title=item.title,
            author=item.author,
            excerpts=excerpts,
            history=data.history,
            message=message,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Chat failed: {e}")

    await mixpanel_service.track("book_chat_message", current_user.id, {
        "item_id": item.id, "mode": item.mode or "wisdom",
    })
    return ChatResponse(reply=reply)
