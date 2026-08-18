from sqlalchemy import Column, String, ForeignKey, DateTime, Integer, Boolean, func
from sqlalchemy.orm import relationship
from app.database import Base


class PushToken(Base):
    __tablename__ = "push_tokens"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String, nullable=False, unique=True)
    platform = Column(String, nullable=True)         # 'ios' | 'android'
    notification_hour = Column(Integer, default=8)   # UTC hour to send daily bite (0-23) — cached/fallback; see notification_local_hour
    notification_minute = Column(Integer, default=0)  # UTC minute (0-55, 5-min steps) — cached/fallback
    # Task 4: the wall-clock time the user actually picked, timezone-independent.
    # `notification_hour`/`notification_minute` above are a UTC CACHE derived
    # from these + users.timezone at write time; the scheduler recomputes the
    # true UTC slot live from these + users.timezone on every tick (see
    # notification_service._effective_utc_slot), so a DST shift or a trip
    # abroad reconciles automatically instead of silently drifting. Null on
    # legacy tokens registered before this field existed — those fall back to
    # the cached UTC columns untouched (pre-existing behavior, unaffected).
    notification_local_hour = Column(Integer, nullable=True)
    notification_local_minute = Column(Integer, nullable=True)
    # Task 4 (Point 5 — "notification settings can lie"): disabling must never
    # erase the token row (re-enabling would otherwise force a full OS
    # permission + re-registration round-trip). This flag is the single
    # source of truth the scheduler gates every send on.
    notifications_enabled = Column(Boolean, default=True)
    streak_alerts_enabled = Column(Boolean, default=True)  # T−65 "streak ends in 1h" push
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="push_tokens")
