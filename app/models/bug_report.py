import uuid
from sqlalchemy import Column, String, Text, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.database import Base


class BugReport(Base):
    """Source of truth for in-app bug reports. The Google Sheet and the
    notification email are convenience mirrors — if either fails, the
    report still lives here."""
    __tablename__ = "bug_reports"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=True)
    where_seen = Column(String, nullable=False)
    description = Column(Text, nullable=False)
    synced_to_sheet = Column(Boolean, default=False)   # made it into the Google Sheet
    emailed = Column(Boolean, default=False)           # notification email sent
    created_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="bug_reports")
