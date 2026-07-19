from sqlalchemy import Column, String, ForeignKey, DateTime, Text, Date, Integer, JSON, func
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
    # ── Session fields (July 2026): per-book card-deck sessions ──
    library_item_id = Column(String, nullable=True, index=True)
    cards = Column(JSON, nullable=True)          # full card deck for the session
    quiz = Column(JSON, nullable=True)           # book-specific quiz (next-day daily quiz + review)
    read_length = Column(Integer, nullable=True) # 5 | 10 | 15 minutes
    mode = Column(String, nullable=True)         # wisdom | story
    chapter = Column(String, nullable=True)      # display line for the home card
    headline = Column(String, nullable=True)
    preview = Column(Text, nullable=True)
    goal_passage = Column(Text, nullable=True)   # this nibble's most goal-relevant excerpt (Connect tab)
    chunk_ids = Column(JSON, nullable=True)      # chunk indexes this session drew from — drives honest Explored % + no-repeat retrieval
    generated_at = Column(DateTime, server_default=func.now())
    # ── Session lifecycle (July 2026): scheduled generation + hold-until-read ──
    origin = Column(String, nullable=True, default="manual")  # 'scheduled' (pre-generated at delivery time) | 'manual' (user tapped a book)
    read_at = Column(DateTime, nullable=True)    # when the user finished reading; NULL = unread/held

    user = relationship("User", back_populates="daily_bites")


class SavedBite(Base):
    __tablename__ = "saved_bites"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    bite_id = Column(String, ForeignKey("daily_bites.id", ondelete="CASCADE"), nullable=False)
    saved_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="saved_bites")
    bite = relationship("DailyBite")
