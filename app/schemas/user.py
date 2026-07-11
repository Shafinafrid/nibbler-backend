from pydantic import BaseModel, EmailStr
from datetime import datetime
from typing import Optional


class UserCreate(BaseModel):
    firebase_uid: str
    email: EmailStr
    display_name: Optional[str] = None


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: Optional[str]
    is_premium: bool
    premium_until: Optional[datetime]
    # Computed tier (subscription OR 7-day trial) — what the app should trust.
    effective_premium: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    # NOTE: deliberately no is_premium here — premium state is only ever
    # written by RevenueCat sync (webhook + /auth/sync-premium), never by
    # a client-supplied value.
    display_name: Optional[str] = None
