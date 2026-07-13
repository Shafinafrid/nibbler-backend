from datetime import datetime, timedelta
from sqlalchemy import Column, String, Boolean, DateTime, func
from sqlalchemy.orm import relationship
from app.database import Base

TRIAL_DAYS = 7                     # Model A: every new signup gets 7 days of Premium
DEV_ALWAYS_PRO = {"b@b.com"}       # dev account — always premium (mirrors the app's __DEV__ shortcut)
DEV_ALWAYS_FREE = {"a@a.com"}      # dev account — always free, trial does NOT apply


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)  # Firebase UID
    email = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    premium_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    @property
    def effective_premium(self) -> bool:
        """The single source of truth for tier gating: a real subscription
        (is_premium / premium_until, once RevenueCat sync lands) OR the
        7-day signup trial. The app computes the same trial client-side —
        without this the backend blocked trial users at the free caps."""
        # Dev-only email overrides — gated to a development env so they never act
        # as a backdoor on the production server. (a@a.com forced-free masked a
        # real RevenueCat purchase during sandbox testing.)
        from app.config import get_settings
        if get_settings().app_env == "development":
            if self.email in DEV_ALWAYS_FREE:
                return False
            if self.email in DEV_ALWAYS_PRO:
                return True
        if self.is_premium:
            return True
        now = datetime.utcnow()
        if self.premium_until and self.premium_until > now:
            return True
        if self.created_at and (now - self.created_at) < timedelta(days=TRIAL_DAYS):
            return True
        return False

    # Relationships
    profile = relationship("Profile", back_populates="user", uselist=False, cascade="all, delete-orphan")
    library_items = relationship("LibraryItem", back_populates="user", cascade="all, delete-orphan")
    daily_bites = relationship("DailyBite", back_populates="user", cascade="all, delete-orphan")
    saved_bites = relationship("SavedBite", back_populates="user", cascade="all, delete-orphan")
    streak = relationship("Streak", back_populates="user", uselist=False, cascade="all, delete-orphan")
    push_tokens = relationship("PushToken", back_populates="user", cascade="all, delete-orphan")
    bug_reports = relationship("BugReport", back_populates="user", cascade="all, delete-orphan")
