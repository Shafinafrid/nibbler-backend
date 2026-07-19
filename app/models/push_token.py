from sqlalchemy import Column, String, ForeignKey, DateTime, Integer, Boolean, func
from sqlalchemy.orm import relationship
from app.database import Base


class PushToken(Base):
    __tablename__ = "push_tokens"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String, nullable=False, unique=True)
    platform = Column(String, nullable=True)         # 'ios' | 'android'
    notification_hour = Column(Integer, default=8)   # UTC hour to send daily bite (0-23)
    notification_minute = Column(Integer, default=0)  # UTC minute (0-55, 5-min steps)
    streak_alerts_enabled = Column(Boolean, default=True)  # T−65 "streak ends in 1h" push
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="push_tokens")
