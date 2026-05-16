from sqlalchemy import Column, String, ForeignKey, DateTime, Text, Date, func
from sqlalchemy.orm import relationship
from app.database import Base


class DailyBite(Base):
    __tablename__ = "daily_bites"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    insight = Column(Text, nullable=False)
    reflection = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    source = Column(String, nullable=True)
    theme = Column(String, nullable=True)
    date = Column(Date, nullable=False)          # The date this bite is for
    generated_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="daily_bites")


class SavedBite(Base):
    __tablename__ = "saved_bites"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    bite_id = Column(String, ForeignKey("daily_bites.id", ondelete="CASCADE"), nullable=False)
    saved_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="saved_bites")
    bite = relationship("DailyBite")
