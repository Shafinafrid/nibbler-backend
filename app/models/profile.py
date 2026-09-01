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
    # Local-first growth profile (July 2026): full mirror of the app's
    # AsyncStorage nibbler_growth_state_v1 blob ({person, profiles[],
    # activeProfileId}) so onboarding survives reinstalls/new devices.
    # The legacy columns above belong to the retired chat-interview onboarding.
    growth_state = Column(JSON, nullable=True)
    # Deletion tombstones (finding #7, Sep 2026): a UNION, never-shrinking set
    # of profile ids that ANY device has ever recorded as deleted. Kept in its
    # OWN column rather than inside growth_state on purpose — growth_state is
    # replaced wholesale by whole-blob LWW on every PUT /profile/growth, so a
    # tombstone living inside it would be just as vulnerable to being
    # clobbered by a stale-but-newer push as the profiles[] array it's meant
    # to protect. A stale device (never opened since a profile was deleted
    # elsewhere) can otherwise push a later timestamp that still carries the
    # deleted profile in its own profiles[] and "win" the LWW compare,
    # resurrecting it. This column is consulted and unioned into (never
    # replaced/shrunk) on every push, independent of growth_state's own
    # LWW outcome — see update_growth_state in app/routers/profile.py.
    deleted_profile_ids = Column(JSON, nullable=True)  # list[str]
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="profile")
