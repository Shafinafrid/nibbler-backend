from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import uuid

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.push_token import PushToken

router = APIRouter(prefix="/notifications", tags=["notifications"])


class RegisterTokenRequest(BaseModel):
    token: str
    platform: Optional[str] = None        # 'ios' | 'android'
    notification_hour: Optional[int] = 8  # UTC hour (0-23)


class RegisterTokenResponse(BaseModel):
    success: bool
    message: str


@router.post("/register", response_model=RegisterTokenResponse)
async def register_push_token(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save (or update) a push token for the authenticated user."""
    if not data.token or not data.token.startswith("ExponentPushToken"):
        raise HTTPException(status_code=400, detail="Invalid Expo push token format.")

    notification_hour = max(0, min(23, data.notification_hour or 8))

    existing = db.query(PushToken).filter(PushToken.token == data.token).first()
    if existing:
        existing.user_id = current_user.id
        existing.platform = data.platform
        existing.notification_hour = notification_hour
    else:
        db.add(PushToken(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            token=data.token,
            platform=data.platform,
            notification_hour=notification_hour,
        ))

    db.commit()
    return {"success": True, "message": "Push token registered."}


@router.delete("/unregister", response_model=RegisterTokenResponse)
async def unregister_push_token(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Remove a push token (call on sign-out or when user disables notifications)."""
    deleted = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .delete()
    )
    db.commit()
    if deleted:
        return {"success": True, "message": "Push token removed."}
    return {"success": False, "message": "Token not found."}


@router.put("/time", response_model=RegisterTokenResponse)
async def update_notification_time(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the preferred delivery hour for an existing push token."""
    notification_hour = max(0, min(23, data.notification_hour or 8))
    rows = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Token not found. Register first.")
    for row in rows:
        row.notification_hour = notification_hour
    db.commit()
    return {"success": True, "message": "Notification time updated."}
