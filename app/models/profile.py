from sqlalchemy import Column, String, ForeignKey, DateTime, JSON, func
from sqlalchemy.orm import relationship
from app.database import Base


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    name = Column(String, nullable=False)
    goals = Column(JSON, nullable=True)          # list of strings
    struggles = Column(String, nullable=True)
    reading_habits = Column(String, nullable=True)
    daily_time = Column(String, nullable=True)
    tone_preference = Column(String, nullable=True)
    background_summary = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="profile")
