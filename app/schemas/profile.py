from pydantic import BaseModel, Field
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
    growth_state: Optional[dict] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GrowthStateUpdate(BaseModel):
    # The app's full nibbler_growth_state_v1 blob: {person, profiles[], activeProfileId}
    growth_state: dict


class OnboardingMessage(BaseModel):
    message: str
    conversation_history: List[dict] = []


class AspirationRequest(BaseModel):
    # Hard length cap: this endpoint is unauthenticated (onboarding runs before
    # account creation), so the input must stay small and cheap.
    answer: str = Field(..., min_length=1, max_length=500)


class AspirationResult(BaseModel):
    # Field names are camelCase on purpose — they mirror the app's GrowthProfile
    # seed shape (see nibbler/src/data/ProfileRepository.js). Defaults make a
    # slightly-off model response still validate instead of 500ing onboarding.
    needsClarification: bool = False
    clarifyPrompt: Optional[str] = None
    lifeArea: str = "Personal Growth"
    contentMode: str = "practical"
    motivation: str = "curiosity"
    motivationType: str = "intrinsic"
    goalOrientation: str = "summary"
    interests: List[str] = []
    profileName: str = "Growing Every Day"
    confirmation: str = ""
    understanding: str = ""


class OnboardingResponse(BaseModel):
    reply: str
    profile: Optional[ProfileCreate] = None
    is_complete: bool = False
