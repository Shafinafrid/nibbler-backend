import tiktoken
from typing import List
from app.config import get_settings

settings = get_settings()

CHUNK_SIZE = 500     # tokens per chunk
CHUNK_OVERLAP = 50   # token overlap between chunks
VOYAGE_MODEL = "voyage-3-lite"   # fast + cheap; upgrade to "voyage-3" for higher accuracy
EMBEDDING_DIM = 512              # voyage-3-lite dimension


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


VOYAGE_BATCH = 128  # Voyage accepts up to 128 texts per embed call

_voyage_client = None


def _get_voyage_client():
    """One client for the process — it was being re-created per chunk."""
    global _voyage_client
    if _voyage_client is None and settings.voyage_api_key:
        import voyageai
        _voyage_client = voyageai.Client(api_key=settings.voyage_api_key)
    return _voyage_client


def _mock_embedding(text: str) -> List[float]:
    """Deterministic local-dev fallback when Voyage is unavailable."""
    import hashlib
    import random
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(EMBEDDING_DIM)]


def _get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    Embed many texts in batches of 128 — a 300-chunk book used to make 300
    separate Voyage calls (one per chunk, each with a fresh client); now it
    makes 3. Uses voyage-3-lite, the most cost-effective model.
    """
    client = _get_voyage_client()
    if client:
        try:
            out: List[List[float]] = []
            for i in range(0, len(texts), VOYAGE_BATCH):
                result = client.embed(texts[i:i + VOYAGE_BATCH], model=VOYAGE_MODEL, input_type="document")
                out.extend(result.embeddings)
            return out
        except Exception as e:
            print(f"[EmbeddingService] Voyage AI error: {e} — falling back to mock")
    return [_mock_embedding(t) for t in texts]


def _get_embedding(text: str) -> List[float]:
    """Single-text convenience wrapper around the batched path."""
    return _get_embeddings([text])[0]


def _get_query_embedding(text: str) -> List[float]:
    """
    Embedding for search queries (uses input_type='query' for better retrieval).
    """
    client = _get_voyage_client()
    if client:
        try:
            result = client.embed([text], model=VOYAGE_MODEL, input_type="query")
            return result.embeddings[0]
        except Exception as e:
            print(f"[EmbeddingService] Voyage AI query error: {e} — falling back to mock")
    return _mock_embedding(text)


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

    def index_text(
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
        embeddings = _get_embeddings(chunks)
        vectors = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            vectors.append({
                "id": f"{item_id}_{i}",
                "values": embedding,
                "metadata": {
                    "user_id": user_id,
                    "item_id": item_id,
                    "chunk_index": i,
                    # Full chunk text (500 tokens ≈ 2KB — well within Pinecone's 40KB
                    # metadata limit). Truncating here starved session generation.
                    "text": chunk[:8000],
                    **(metadata or {}),
                },
            })

        # Upsert in batches of 100
        batch_size = 100
        for i in range(0, len(vectors), batch_size):
            self.index.upsert(vectors=vectors[i:i + batch_size], namespace=user_id)

        return len(vectors)

    def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
    ) -> List[str]:
        """Search Pinecone for relevant content chunks for a given query."""
        if not self.pinecone_available:
            return []

        query_embedding = _get_query_embedding(query)
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=user_id,
            include_metadata=True,
        )
        return [match.metadata.get("text", "") for match in results.matches]

    def search_item(
        self,
        query: str,
        user_id: str,
        item_id: str,
        top_k: int = 8,
    ) -> List[str]:
        """Search within ONE library item's chunks (for per-book sessions)."""
        if not self.pinecone_available:
            return []

        query_embedding = _get_query_embedding(query)
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=user_id,
            filter={"item_id": {"$eq": item_id}},
            include_metadata=True,
        )
        return [match.metadata.get("text", "") for match in results.matches]

    def search_item_scored(
        self,
        query: str,
        user_id: str,
        item_id: str,
        top_k: int = 8,
    ):
        """Like search_item but returns [(text, score)] — scores drive the
        Connect tab's goal-relevance analytics."""
        if not self.pinecone_available:
            return []

        query_embedding = _get_query_embedding(query)
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=user_id,
            filter={"item_id": {"$eq": item_id}},
            include_metadata=True,
        )
        return [(m.metadata.get("text", ""), float(m.score or 0)) for m in results.matches]

    def fetch_chunks(
        self,
        item_id: str,
        user_id: str,
        start: int,
        count: int,
    ) -> List[str]:
        """Fetch sequential chunks by index (story mode reads the book in order)."""
        if not self.pinecone_available or count <= 0:
            return []
        ids = [f"{item_id}_{i}" for i in range(start, start + count)]
        try:
            res = self.index.fetch(ids=ids, namespace=user_id)
            vectors = getattr(res, "vectors", None) or {}
            out = []
            for vid in ids:  # preserve reading order
                v = vectors.get(vid)
                if v is not None and getattr(v, "metadata", None):
                    out.append(v.metadata.get("text", ""))
            return [t for t in out if t]
        except Exception as e:
            print(f"[EmbeddingService] fetch_chunks error: {e}")
            return []

    def delete_item_vectors(self, item_id: str, user_id: str = None):
        """Delete all vectors for a given library item."""
        if not self.pinecone_available:
            return
        try:
            if user_id:
                self.index.delete(
                    filter={"item_id": {"$eq": item_id}},
                    namespace=user_id,
                )
            else:
                self.index.delete(filter={"item_id": {"$eq": item_id}})
        except Exception as e:
            print(f"[EmbeddingService] Delete error: {e}")

    def delete_user_namespace(self, user_id: str):
        """Delete ALL vectors for a user (entire namespace). Used on account deletion."""
        if not self.pinecone_available:
            return
        try:
            self.index.delete(delete_all=True, namespace=user_id)
        except Exception as e:
            print(f"[EmbeddingService] Namespace delete error: {e}")
