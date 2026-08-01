from sqlalchemy import Column, String, ForeignKey, DateTime, Boolean, Integer, Text, JSON, func
from sqlalchemy.orm import relationship
from app.database import Base


class LibraryItem(Base):
    __tablename__ = "library_items"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    # Figures extracted from the source at upload — see services/image_extract.
    # [{ref, page, w, h, context}]; empty for books with no usable pictures.
    images = Column(JSON, nullable=True)
    # Scanned PDFs: 'needed' once extraction finds no text, then 'running' /
    # 'done' / 'failed'. Null for every book with a real text layer.
    ocr_status = Column(String, nullable=True)
    ocr_pages_done = Column(Integer, default=0)
    ocr_pages_total = Column(Integer, default=0)
    type = Column(String, nullable=False)       # pdf | url | text | note
    content = Column(Text, nullable=True)         # raw text or pasted content
    file_url = Column(String, nullable=True)      # S3 object key (pre-July-2026 rows hold full public URLs)
    file_size = Column(Integer, nullable=True)    # bytes
    source_url = Column(String, nullable=True)    # original URL for scraped articles
    processed = Column(Boolean, default=False)
    chunk_count = Column(Integer, default=0)      # number of Pinecone vectors
    processing_error = Column(String, nullable=True)  # error message if processing failed
    # Whether the ORIGINAL uploaded file actually reached S3. `processed` only
    # ever meant "text extracted + indexing attempted", but it was being read
    # as if it also implied the file was archived — so a silent S3 failure left
    # a row that looked healthy with no original behind it.
    #   None    → pre-2026-07-26 row, unknown
    #   stored  → the original is in S3 at file_url
    #   failed  → archival failed; file_url is NULL and nothing retried
    archive_status = Column(String, nullable=True)
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
