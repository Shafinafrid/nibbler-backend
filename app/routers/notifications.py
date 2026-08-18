from datetime import datetime
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
    platform: Optional[str] = None          # 'ios' | 'android'
    notification_hour: Optional[int] = 8    # UTC hour (0-23) — cached fallback, see local fields
    notification_minute: Optional[int] = 0  # UTC minute — snapped to 5-min steps
    # Task 4: the wall-clock time the user actually picked, timezone-independent.
    # Optional so older app builds that don't send it yet keep working exactly
    # as before (falls back to the UTC-only fields above).
    notification_local_hour: Optional[int] = None
    notification_local_minute: Optional[int] = None
    streak_alerts: Optional[bool] = None    # None = leave the stored value alone


class StreakAlertsRequest(BaseModel):
    token: str
    enabled: bool


class SetEnabledRequest(BaseModel):
    token: str
    enabled: bool


def _clamp_time(data: RegisterTokenRequest) -> tuple:
    hour = max(0, min(23, data.notification_hour or 8))
    # Scheduler ticks every 5 minutes, so snap to the slot it will check.
    minute = max(0, min(59, data.notification_minute or 0))
    minute = (minute // 5) * 5
    return hour, minute


def _clamp_local_time(data: RegisterTokenRequest) -> tuple:
    """Same clamping as _clamp_time, for the local wall-clock fields —
    None stays None (means "not sent by this client build")."""
    if data.notification_local_hour is None:
        return None, None
    hour = max(0, min(23, data.notification_local_hour))
    minute = max(0, min(59, data.notification_local_minute or 0))
    minute = (minute // 5) * 5
    return hour, minute


class RegisterTokenResponse(BaseModel):
    success: bool
    message: str


@router.post("/register", response_model=RegisterTokenResponse)
def register_push_token(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save (or update) a push token for the authenticated user."""
    if not data.token or not data.token.startswith("ExponentPushToken"):
        raise HTTPException(status_code=400, detail="Invalid Expo push token format.")

    notification_hour, notification_minute = _clamp_time(data)
    local_hour, local_minute = _clamp_local_time(data)

    existing = db.query(PushToken).filter(PushToken.token == data.token).first()
    if existing:
        existing.user_id = current_user.id
        existing.platform = data.platform
        existing.notification_hour = notification_hour
        existing.notification_minute = notification_minute
        if local_hour is not None:
            existing.notification_local_hour = local_hour
            existing.notification_local_minute = local_minute
        if data.streak_alerts is not None:
            existing.streak_alerts_enabled = data.streak_alerts
        # Registering (including re-registering an existing token) is always
        # an explicit "I want notifications" action — Task 4 item 8: an
        # enable must be as real/confirmed as a disable, never assumed.
        existing.notifications_enabled = True
    else:
        db.add(PushToken(
            id=str(uuid.uuid4()),
            user_id=current_user.id,
            token=data.token,
            platform=data.platform,
            notification_hour=notification_hour,
            notification_minute=notification_minute,
            notification_local_hour=local_hour,
            notification_local_minute=local_minute,
            notifications_enabled=True,
            streak_alerts_enabled=True if data.streak_alerts is None else data.streak_alerts,
        ))

    db.commit()
    return {"success": True, "message": "Push token registered."}


@router.delete("/unregister", response_model=RegisterTokenResponse)
def unregister_push_token(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Hard-remove a push token — for sign-out / switching accounts on this
    device, where the token genuinely should stop being this user's. NOT
    used for the in-app notifications toggle anymore (see PUT /enabled) —
    Task 4 item 7: disabling from Settings must never erase the token, or
    turning it back on forces a full OS-permission + re-registration
    round-trip for no reason."""
    deleted = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .delete()
    )
    db.commit()
    if deleted:
        return {"success": True, "message": "Push token removed."}
    return {"success": False, "message": "Token not found."}


@router.put("/enabled", response_model=RegisterTokenResponse)
def set_notifications_enabled(
    data: SetEnabledRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Task 4 items 7/8: the real backend of the Profile toggle. Flips
    `notifications_enabled` without deleting the row — the scheduler
    (`_notify_delivery_slot`/`_notify_streak_alert_slot`) gates every send
    on this flag, so a disabled token is guaranteed to receive nothing
    while it stays disabled, and re-enabling needs no new OS permission
    prompt or token re-issue."""
    rows = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Token not found. Register first.")
    for row in rows:
        row.notifications_enabled = data.enabled
    db.commit()
    return {"success": True, "message": "Notifications enabled." if data.enabled else "Notifications disabled."}


class NotificationStateResponse(BaseModel):
    registered: bool
    enabled: bool
    notification_local_hour: Optional[int] = None
    notification_local_minute: Optional[int] = None
    notification_hour: Optional[int] = None
    notification_minute: Optional[int] = None
    streak_alerts_enabled: bool = True
    timezone: Optional[str] = None


@router.get("/state", response_model=NotificationStateResponse)
def get_notification_state(
    token: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Task 4 item 9: the CONFIRMED device state as the backend actually has
    it — not whatever the app last optimistically wrote to local storage.
    The Profile screen reconciles against this on open and on app-resume
    (item 10), so a setting change that silently failed to save server-side
    can never keep showing as "on" / "at this time" client-side forever."""
    row = (
        db.query(PushToken)
        .filter(PushToken.token == token, PushToken.user_id == current_user.id)
        .first()
    )
    if not row:
        return NotificationStateResponse(registered=False, enabled=False, timezone=current_user.timezone)
    return NotificationStateResponse(
        registered=True,
        enabled=bool(row.notifications_enabled),
        notification_local_hour=row.notification_local_hour,
        notification_local_minute=row.notification_local_minute,
        notification_hour=row.notification_hour,
        notification_minute=row.notification_minute,
        streak_alerts_enabled=bool(row.streak_alerts_enabled),
        timezone=current_user.timezone,
    )


@router.put("/streak-alerts", response_model=RegisterTokenResponse)
def update_streak_alerts(
    data: StreakAlertsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Toggle the T−65 'streak ends in 1 hour' push for the user's token(s)."""
    rows = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Token not found. Register first.")
    for row in rows:
        row.streak_alerts_enabled = data.enabled
    db.commit()
    return {"success": True, "message": "Streak alerts updated."}


class SendTestResponse(BaseModel):
    success: bool
    title: str
    body: str


@router.post("/send-test", response_model=SendTestResponse)
async def send_test_notification(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Owner-facing test button: sends the CURRENT user a real push, right
    now, using their actual current state — not a canned example. Reuses
    the exact same dynamic, book-specific title/body/data selection the
    real scheduled delivery and streak-alert pushes use
    (`preview_notification_for_user`), just without waiting for their
    configured delivery time or the T-65 streak slot. A real Expo push
    carrying the real deep-link payload, so what arrives on-device — and
    what tapping it does — matches production exactly."""
    from app.services.notification_service import (
        preview_notification_for_user, send_push_notifications,
    )
    from app.config import get_settings

    tokens = [
        t.token for t in
        db.query(PushToken).filter(PushToken.user_id == current_user.id).all()
    ]
    if not tokens:
        raise HTTPException(
            status_code=400,
            detail="No push token registered on this device yet — enable notifications first.",
        )

    try:
        title, body, data = preview_notification_for_user(db, current_user, datetime.utcnow())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    settings = get_settings()
    await send_push_notifications(
        tokens=tokens,
        title=title,
        body=body,
        data=data,
        expo_access_token=getattr(settings, "expo_access_token", ""),
    )
    return {"success": True, "title": title, "body": body}


@router.put("/time", response_model=RegisterTokenResponse)
def update_notification_time(
    data: RegisterTokenRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update the preferred delivery time for an existing token. Stores both
    the UTC cache (back-compat) and, when sent, the LOCAL wall-clock time —
    the scheduler recomputes the true UTC slot from the local time + the
    user's current timezone on every tick (Task 4 items 13/14), so this
    endpoint no longer needs to be called again just because the user's
    timezone changed."""
    notification_hour, notification_minute = _clamp_time(data)
    local_hour, local_minute = _clamp_local_time(data)
    rows = (
        db.query(PushToken)
        .filter(PushToken.token == data.token, PushToken.user_id == current_user.id)
        .all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Token not found. Register first.")
    for row in rows:
        row.notification_hour = notification_hour
        row.notification_minute = notification_minute
        if local_hour is not None:
            row.notification_local_hour = local_hour
            row.notification_local_minute = local_minute
    db.commit()
    return {"success": True, "message": "Notification time updated."}
