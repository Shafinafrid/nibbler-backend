from sqlalchemy import Column, String, ForeignKey, DateTime, Boolean, Integer, Text, func
from sqlalchemy.orm import relationship
from app.database import Base


class LibraryItem(Base):
    __tablename__ = "library_items"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    type = Column(String, nullable=False)       # pdf | url | text | note
    content = Column(Text, nullable=True)         # raw text or pasted content
    file_url = Column(String, nullable=True)      # S3 object key (pre-July-2026 rows hold full public URLs)
    file_size = Column(Integer, nullable=True)    # bytes
    source_url = Column(String, nullable=True)    # original URL for scraped articles
    processed = Column(Boolean, default=False)
    chunk_count = Column(Integer, default=0)      # number of Pinecone vectors
    processing_error = Column(String, nullable=True)  # error message if processing failed
    # ── Nibble-session fields (July 2026) ──
    mode = Column(String, default="wisdom")            # wisdom | story
    kind = Column(String, default="book")              # book | article | paper
    author = Column(String, nullable=True)
    growth_profile_name = Column(String, nullable=True)  # premium: which profile this feeds
    story_progress = Column(Integer, default=0)          # story mode: next chunk index to read
    is_active = Column(Boolean, default=True)            # feeds nibble generation (≤5 active per user)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="library_items")
