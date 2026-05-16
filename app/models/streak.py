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
    total_bites_read = Column(Integer, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="streak")
