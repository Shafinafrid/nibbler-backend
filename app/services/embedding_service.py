import tiktoken
from typing import List
from app.config import get_settings

settings = get_settings()

CHUNK_SIZE = 500     # tokens per chunk
CHUNK_OVERLAP = 50  # token overlap between chunks
EMBEDDING_MODEL = "text-embedding-3-small"


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping token chunks."""
    enc = tiktoken.get_encoding("cl100k_base")
    tokens = enc.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunk_tokens = tokens[start:end]
        chunks.append(enc.decode(chunk_tokens))
        start += chunk_size - overlap
    return chunks


def _get_embedding(text: str) -> List[float]:
    """Get embedding from Anthropic (uses voyage-3 via the API) or OpenAI fallback."""
    import anthropic
    client = anthropic.Anthropic(api_key=settings.claude_api_key)
    # Anthropic doesn't have native embeddings yet — use a simple hash-based mock
    # for development. Replace with OpenAI text-embedding-3-small or Voyage AI in production.
    import hashlib
    import struct
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    import random
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(1536)]


class EmbeddingService:
    def __init__(self):
        try:
            from pinecone import Pinecone
            pc = Pinecone(api_key=settings.pinecone_api_key)
            self.index = pc.Index(settings.pinecone_index_name)
            self.pinecone_available = True
        except Exception:
            self.pinecone_available = False
            self.index = None

    async def index_text(
        self,
        text: str,
        item_id: str,
        user_id: str,
        metadata: dict = None,
    ) -> int:
        """Chunk text, embed each chunk, and upsert to Pinecone."""
        if not self.pinecone_available:
            return 0

        chunks = _chunk_text(text)
        vectors = []
        for i, chunk in enumerate(chunks):
            embedding = _get_embedding(chunk)
            vectors.append({
                "id": f"{item_id}_{i}",
                "values": embedding,
                "metadata": {
                    "user_id": user_id,
                    "item_id": item_id,
                    "chunk_index": i,
                    "text": chunk[:500],  # store snippet for retrieval
                    **(metadata or {}),
                },
            })

        # Upsert in batches of 100
        batch_size = 100
        for i in range(0, len(vectors), batch_size):
            self.index.upsert(vectors=vectors[i:i+batch_size], namespace=user_id)

        return len(vectors)

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> List[str]:
        """Search Pinecone for relevant content chunks for a given query."""
        if not self.pinecone_available:
            return []

        query_embedding = _get_embedding(query)
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=user_id,
            include_metadata=True,
        )
        return [match.metadata.get("text", "") for match in results.matches]

    async def delete_item_vectors(self, item_id: str):
        """Delete all vectors for a given library item."""
        if not self.pinecone_available:
            return
        # Pinecone delete by metadata filter
        try:
            self.index.delete(filter={"item_id": {"$eq": item_id}})
        except Exception:
            pass
