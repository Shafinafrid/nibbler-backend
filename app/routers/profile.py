from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.profile import Profile
from app.rate_limit import limiter
from app.schemas.profile import (
    ProfileCreate,
    ProfileUpdate,
    ProfileResponse,
    AspirationRequest,
    AspirationResult,
    GrowthStateUpdate,
)
from app.services.claude import ClaudeService
import uuid

router = APIRouter(prefix="/profile", tags=["profile"])


def _get_or_create_profile(user: User, db: Session) -> Profile:
    """Local-first onboarding never creates a backend profile row, so the
    row is created lazily — a 404 here used to break every local-onboarded
    user (and legacy /bites/today)."""
    if user.profile:
        return user.profile
    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=user.display_name or (user.email or "").split("@")[0] or "Nibbler user",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    db.refresh(user)
    return profile


@router.get("/", response_model=ProfileResponse)
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return _get_or_create_profile(current_user, db)


@router.put("/growth", response_model=ProfileResponse)
def update_growth_state(
    data: GrowthStateUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store the app's full local growth state ({person, profiles[],
    activeProfileId}) so onboarding survives reinstalls and new devices.
    The app pushes on every ProfileRepository.saveState and pulls when its
    local copy is empty at sign-in."""
    profile = _get_or_create_profile(current_user, db)
    profile.growth_state = data.growth_state
    person_name = ((data.growth_state.get("person") or {}).get("name") or "").strip()
    if person_name:
        profile.name = person_name
    db.commit()
    db.refresh(profile)
    return profile


@router.put("/", response_model=ProfileResponse)
def update_profile(
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
def complete_onboarding(
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


@router.post("/interpret-aspiration", response_model=AspirationResult)
@limiter.limit("10/hour")  # per-IP (unauthenticated); ~3 calls per real onboarding
def interpret_aspiration(request: Request, data: AspirationRequest):
    """
    Onboarding aspiration interpreter (moved server-side July 2026 so the
    Anthropic key never ships in the app binary).

    Deliberately unauthenticated: onboarding runs before account creation.
    Kept cheap and abuse-resistant via the 500-char input cap, the free-tier
    model, small max_tokens, and per-IP rate limiting.
    """
    claude = ClaudeService(is_premium=False)
    return claude.interpret_aspiration(data.answer)


# NOTE (July 2026): POST /profile/onboarding/chat was retired here. It served
# the old conversational-interview onboarding, which the app replaced with the
# local-first aspiration flow (POST /profile/interpret-aspiration above).
