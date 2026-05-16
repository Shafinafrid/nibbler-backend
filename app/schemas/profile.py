from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List


class ProfileCreate(BaseModel):
    name: str
    goals: Optional[List[str]] = None
    struggles: Optional[str] = None
    reading_habits: Optional[str] = None
    daily_time: Optional[str] = None
    tone_preference: Optional[str] = None
    background_summary: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    goals: Optional[List[str]] = None
    struggles: Optional[str] = None
    reading_habits: Optional[str] = None
    daily_time: Optional[str] = None
    tone_preference: Optional[str] = None
    background_summary: Optional[str] = None


class ProfileResponse(BaseModel):
    id: str
    user_id: str
    name: str
    goals: Optional[List[str]]
    struggles: Optional[str]
    reading_habits: Optional[str]
    daily_time: Optional[str]
    tone_preference: Optional[str]
    background_summary: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class OnboardingMessage(BaseModel):
    message: str
    conversation_history: List[dict] = []


class OnboardingResponse(BaseModel):
    reply: str
    profile: Optional[ProfileCreate] = None
    is_complete: bool = False
