from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.profile import Profile
from app.schemas.profile import ProfileCreate, ProfileUpdate, ProfileResponse, OnboardingMessage, OnboardingResponse
from app.services.claude import ClaudeService
import uuid

router = APIRouter(prefix="/profile", tags=["profile"])


@router.get("/", response_model=ProfileResponse)
async def get_profile(current_user: User = Depends(get_current_user)):
    if not current_user.profile:
        raise HTTPException(status_code=404, detail="Profile not found. Complete onboarding first.")
    return current_user.profile


@router.put("/", response_model=ProfileResponse)
async def update_profile(
    data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user.profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(current_user.profile, field, value)

    db.commit()
    db.refresh(current_user.profile)
    return current_user.profile


@router.post("/complete-onboarding", response_model=ProfileResponse)
async def complete_onboarding(
    data: ProfileCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the completed onboarding profile."""
    if current_user.profile:
        # Update existing profile
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(current_user.profile, field, value)
        db.commit()
        db.refresh(current_user.profile)
        return current_user.profile

    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        **data.model_dump(exclude_none=True),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/onboarding/chat", response_model=OnboardingResponse)
async def onboarding_chat(
    data: OnboardingMessage,
    current_user: User = Depends(get_current_user),
):
    """
    Send a message to Nibbler during onboarding.
    Returns Nibbler's reply and optionally a completed profile.
    """
    claude = ClaudeService(is_premium=current_user.effective_premium)
    result = await claude.onboarding_reply(
        conversation_history=data.conversation_history,
        user_message=data.message,
    )
    return result
