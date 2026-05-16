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


class StreakResponse(BaseModel):
    current_streak: int
    longest_streak: int
    last_active_date: Optional[date]
    total_bites_read: int

    class Config:
        from_attributes = True
