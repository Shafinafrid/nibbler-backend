from datetime import datetime, timedelta
from sqlalchemy import Column, String, Boolean, DateTime, Integer, BigInteger, func
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

    # ── Canonical entitlement provenance (Task 8, Aug 2026) ───────────────────
    # `is_premium`/`premium_until` alone can't say WHY a grant exists — a real
    # subscription and a founder/tester promotional grant look identical.
    # Written explicitly by the ONE place that grants access (the RevenueCat
    # webhook, and sync-premium's REST read) — never inferred elsewhere.
    # 'paid' | 'complimentary' | None (nothing currently active/never set).
    entitlement_source = Column(String, nullable=True)
    # Permanent, monotonic — set True the FIRST time a REAL (non-promotional)
    # purchase is observed, and never cleared. Answers "has this account ever
    # held paid access", independent of trial_anchor_at (which only governs
    # whether the signup trial can re-open).
    has_held_paid_entitlement = Column(Boolean, default=False, nullable=False)
    # Last time an entitlement write path (webhook OR sync-premium) actually
    # confirmed state with RevenueCat — surfaced in the entitlement resolver
    # so a client can tell "confirmed 2 minutes ago" from "never synced".
    premium_synced_at = Column(DateTime, nullable=True)
    # Idempotency/ordering guard for the webhook specifically (Task 8): RC
    # webhooks carry no per-user sequence number, only their own event id +
    # timestamp — both must be tracked to reject an exact-duplicate redelivery
    # AND a genuinely-older event arriving after a newer one was already
    # applied (retries reuse the same id/timestamp per RC's own docs, so a
    # duplicate is caught by id and a stale reorder is caught by timestamp).
    last_webhook_event_id = Column(String, nullable=True)
    last_webhook_event_at_ms = Column(BigInteger, nullable=True)  # ms epoch — too large for 32-bit INTEGER

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
    device_model = Column(String, nullable=True)   # e.g. "iPhone 15 Pro", "Pixel 7" (expo-device)
    os_version = Column(String, nullable=True)      # e.g. "17.5", "14"
    last_seen_at = Column(DateTime, nullable=True)

    # Task 3 fix (Aug 2026): which unread bite is currently "the" featured
    # nibble for this user's reminder pushes — see notification_service.
    # _feature_bite(). Before this, the featured pick among several unread
    # candidates (a Premium account can have more than one active source's
    # nibble unread at once) was a PURE function of today's date, so it
    # could silently switch to a different still-unread book day to day —
    # violating the task's own explicit rule ("do not advance to another
    # book until the current featured nibble is read"). No FK: a deleted/
    # since-read bite just means this pointer is stale and gets replaced on
    # the next pick, exactly like `LibraryItem.last_processing_attempt_id`'s
    # own no-FK precedent elsewhere in this codebase.
    last_featured_bite_id = Column(String, nullable=True)

    # Trial anchor, SEPARATE from created_at (Task 7, Aug 2026). Normally
    # equal to created_at, but seeded from email_account_history.trial_anchor_at
    # on a fresh row for an email that has been through account deletion
    # before — see get_or_create_user. Keeping this distinct from created_at
    # means created_at always means "this row's real creation time" (useful
    # for records/support), while trial eligibility can be backdated without
    # touching that meaning.
    trial_anchor_at = Column(DateTime, nullable=True)

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
        anchor = self.trial_anchor_at or self.created_at
        if anchor and (now - anchor) < timedelta(days=TRIAL_DAYS):
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


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


class EmailAccountHistory(Base):
    """Task 7 (Aug 2026): a durable, per-email anti-abuse guard that
    deliberately OUTLIVES account deletion — no ForeignKey to users.id, same
    survives-erasure pattern as AccountErasure/CleanupTask in library.py.

    Deleting an account must give the user a genuinely fresh, empty account
    on re-signup with the same email — but it must NOT reset their 7-day
    signup trial or their lifetime free-upload count, or "delete and
    recreate" becomes an unlimited-trials/unlimited-free-uploads loop.

    Written ONCE, atomically with the user row's deletion, only on a FULLY
    successful erasure (see _attempt_account_erasure_cleanup in
    routers/auth.py) — there is no earlier point at which the same email
    could be reused anyway, since Firebase itself refuses a duplicate email
    until the old Firebase Auth record is actually gone.

    Read on every new-row signup in get_or_create_user (middleware/auth.py)
    to seed the new row's trial_anchor_at / successful_sources_total instead
    of leaving them at their fresh defaults.
    """
    __tablename__ = "email_account_history"

    email = Column(String, primary_key=True)  # normalize_email() form
    trial_anchor_at = Column(DateTime, nullable=False)
    lifetime_successful_sources_total = Column(Integer, default=0, nullable=False)
    first_seen_user_id = Column(String, nullable=True)  # informational only
    deletions_count = Column(Integer, default=1, nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
