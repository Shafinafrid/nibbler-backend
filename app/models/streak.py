from sqlalchemy import Column, String, ForeignKey, DateTime, Integer, Date, func
from sqlalchemy.orm import relationship
from app.database import Base


class Streak(Base):
    __tablename__ = "streaks"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    current_streak = Column(Integer, default=0)
    longest_streak = Column(Integer, default=0)
    last_active_date = Column(Date, nullable=True)
    # Exact completion timestamp (UTC). last_active_date is only a DATE, which
    # can't tell "read at 10:15, inside the closing window" from "read at 11:30,
    # after it closed" — the cycle-boundary streak math needs the real moment.
    last_completed_at = Column(DateTime, nullable=True)
    # Task 20 — per-day idempotency + bounded-catch-up marker for the streak
    # alert (see notification_service._notify_streak_alert_slot). Without
    # this, widening the exact-slot match to a bounded window (so a missed
    # T-65 tick can still catch up within STREAK_ALERT_CATCHUP_WINDOW) would
    # let the SAME alert re-match and re-send on every tick inside that
    # window, not just once.
    last_alert_sent_date = Column(Date, nullable=True)
    total_bites_read = Column(Integer, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="streak")
