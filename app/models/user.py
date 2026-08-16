from datetime import datetime, timedelta
from sqlalchemy import Column, String, Boolean, DateTime, Integer, func
from sqlalchemy.orm import relationship
from app.database import Base

TRIAL_DAYS = 7                     # Model A: every new signup gets 7 days of Premium


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)  # Firebase UID
    email = Column(String, unique=True, nullable=False, index=True)
    display_name = Column(String, nullable=True)
    is_premium = Column(Boolean, default=False)
    premium_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ── Identity + device context (July 2026) ────────────────────────────────
    # Everything here is either user-supplied or a technical attribute of their
    # own session. Deliberately NOT collected: precise location, contacts,
    # advertising identifiers, or anything requiring a consent prompt we don't
    # show — those would change the App Store privacy labels and need a legal
    # basis we haven't established.
    username = Column(String, unique=True, nullable=True, index=True)
    avatar_url = Column(String, nullable=True)     # S3 object KEY (private bucket)
    timezone = Column(String, nullable=True)       # IANA, e.g. "Europe/Stockholm"
    locale = Column(String, nullable=True)         # e.g. "en-GB"
    platform = Column(String, nullable=True)       # 'ios' | 'android'
    app_version = Column(String, nullable=True)
    last_seen_at = Column(DateTime, nullable=True)

    # ── Free lifetime source entitlement (Task 2, Aug 2026) ───────────────────
    # A PERMANENT, monotonic count of library_items that have ever reached
    # processed=True for this account — regardless of tier at the time, and
    # NEVER decremented on delete. This is what "3 permanent successful
    # sources" is checked against for Free uploads; it is deliberately NOT a
    # live COUNT(*) of current rows, because deleting a source must not
    # restore an entitlement. See app/services/entitlement_service.py.
    successful_sources_total = Column(Integer, default=0, nullable=False)
    # Transient count of currently-PENDING Free reservations (see
    # entitlement_service.reserve_free_capacity) — a slot held between "an
    # upload started, capacity was checked" and "processing finished/failed".
    # Always 0 for an entitled account. Never a substitute for
    # successful_sources_total: reserved converts into consumed (+1 there,
    # -1 here) on success, or is released (-1 here, nothing consumed) on
    # failure/expiry — see app/services/entitlement_service.py.
    reserved_sources_count = Column(Integer, default=0, nullable=False)
    # Identifies the entitlement "episode" (is_premium + premium_until) that
    # library_items.is_unlocked_selection was last computed for. Reconciled
    # lazily on every authenticated request (see middleware/auth.py) — NULL
    # means "never reconciled yet".
    free_lock_state_token = Column(String, nullable=True)
    # Snapshot of `effective_premium` as of the last reconcile call. Needed
    # because `free_lock_state_token` alone cannot detect a PURE wall-clock
    # transition (trial or premium_until expiring): `is_premium`/
    # `premium_until` don't change merely because time passed, so the token
    # stays identical across the exact moment effective_premium flips
    # True→False. Comparing against this snapshot is what makes that
    # transition detectable — see entitlement_service.reconcile_free_lock_state.
    free_lock_last_effective_premium = Column(Boolean, nullable=True)

    @property
    def effective_premium(self) -> bool:
        """The single source of truth for tier gating: a real subscription
        (is_premium / premium_until, once RevenueCat sync lands) OR the
        7-day signup trial. The app computes the same trial client-side —
        without this the backend blocked trial users at the free caps."""
        if self.is_premium:
            return True
        now = datetime.utcnow()
        if self.premium_until and self.premium_until > now:
            return True
        # A lapsed subscriber (premium_until set but in the past) lands on the
        # FREE tier — the signup trial never resumes after a real subscription.
        # The RC webhook/sync keep the expired timestamp instead of nulling it
        # precisely so this check works.
        if self.premium_until:
            return False
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
    # Restored wholesale on a new device — see routers/sync.py
    notes = relationship("Note", back_populates="user", cascade="all, delete-orphan")
    highlights = relationship("Highlight", back_populates="user", cascade="all, delete-orphan")
    chat_messages = relationship("ChatMessage", back_populates="user", cascade="all, delete-orphan")
    completions = relationship("Completion", back_populates="user", cascade="all, delete-orphan")
    settings = relationship("UserSettings", back_populates="user", uselist=False, cascade="all, delete-orphan")
    state = relationship("UserState", back_populates="user", uselist=False, cascade="all, delete-orphan")
