from sqlalchemy import Column, String, ForeignKey, DateTime, Boolean, Integer, Text, func
from sqlalchemy.orm import relationship
from app.database import Base


class LibraryItem(Base):
    __tablename__ = "library_items"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    type = Column(String, nullable=False)       # pdf | url | text | note
    content = Column(Text, nullable=True)       # raw text or URL
    file_url = Column(String, nullable=True)    # S3 URL for PDFs
    file_size = Column(Integer, nullable=True)  # bytes
    processed = Column(Boolean, default=False)
    chunk_count = Column(Integer, default=0)    # number of Pinecone vectors
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="library_items")
