from pydantic import BaseModel
from datetime import datetime, date
from typing import Optional, List


class BiteResponse(BaseModel):
    id: str
    title: str
    insight: str
    reflection: str
    action: str
    source: Optional[str]
    theme: Optional[str]
    date: date
    is_saved: bool = False

    class Config:
        from_attributes = True


class SavedBiteResponse(BaseModel):
    id: str
    bite: BiteResponse
    saved_at: datetime

    class Config:
        from_attributes = True


class BiteHistoryResponse(BaseModel):
    bites: List[BiteResponse]
    total: int


class SessionHistoryItem(BaseModel):
    """A past session WITH its deck — enough to rebuild the local cache.

    `BiteResponse` deliberately omits `cards` and `quiz` (it serves the legacy
    single-insight shape), which is why the Review screen could not be rebuilt
    after a reinstall: the quizzes were sitting in `daily_bites` the whole time
    with no route that returned them.
    """
    id: str
    library_item_id: Optional[str] = None
    date: date
    mode: Optional[str] = None
    read_length: Optional[int] = None
    title: str
    chapter: Optional[str] = None
    headline: Optional[str] = None
    preview: Optional[str] = None
    cards: List = []
    quiz: Optional[List] = None
    goal_passage: Optional[str] = None
    read_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SessionHistoryResponse(BaseModel):
    sessions: List[SessionHistoryItem]
    total: int


class StreakResponse(BaseModel):
    current_streak: int
    longest_streak: int
    last_active_date: Optional[date]
    total_bites_read: int

    class Config:
        from_attributes = True
