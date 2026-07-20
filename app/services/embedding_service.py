import math
import tiktoken
from typing import List, Optional
from app.config import get_settings

settings = get_settings()


class EmbeddingError(Exception):
    """Voyage failed while a key IS configured. Never swallowed into mock
    vectors: one silently-mocked book poisons its Pinecone entries with random
    vectors, and the Connect goal-match reads a nonsense ~4% forever."""

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
    """One client for the process — it was being re-created per chunk.
    max_retries handles transient rate limits / 5xx with backoff."""
    global _voyage_client
    if _voyage_client is None and settings.voyage_api_key:
        import voyageai
        _voyage_client = voyageai.Client(api_key=settings.voyage_api_key, max_retries=5)
    return _voyage_client


def _mock_embedding(text: str) -> List[float]:
    """Deterministic stand-in used ONLY when no Voyage key is configured
    (keyless local dev). A failing key raises EmbeddingError instead — silent
    mock fallback is what poisoned production vectors in July 2026."""
    import hashlib
    import random
    seed = int(hashlib.md5(text.encode()).hexdigest(), 16)
    rng = random.Random(seed)
    return [rng.uniform(-1, 1) for _ in range(EMBEDDING_DIM)]


def embedding_provider() -> str:
    """Which provider vectors from this process come from ('voyage'|'mock')."""
    return "voyage" if settings.voyage_api_key else "mock"


def _embed_batch_with_backoff(client, batch: List[str], input_type: str, max_attempts: int = 6, base_delay: int = 5):
    """
    Manual retry on top of the client's own max_retries: without a payment
    method on the Voyage account, the limit is 3 RPM / 10K TPM (documented,
    LAUNCH_CHECKLIST §3b) — a book with several batches back-to-back WILL hit
    that ceiling. Background ingestion has no request timeout, so the default
    budget waits out real rate limits (429) with growing backoff (5s, 10s,
    20s, 40s, 80s, 160s ≈ 5 min total) instead of failing the whole book.
    Live request paths (chat/insights) pass a shorter base_delay + fewer
    attempts so they don't block the HTTP response for minutes. Non-rate-limit
    errors (bad key, malformed request) fail immediately — retrying is pointless.
    """
    import time
    delay = base_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return client.embed(batch, model=VOYAGE_MODEL, input_type=input_type)
        except Exception as e:
            is_rate_limit = '429' in str(e) or 'rate limit' in str(e).lower()
            if not is_rate_limit or attempt == max_attempts:
                raise
            time.sleep(delay)
            delay *= 2


def _get_embeddings(texts: List[str]) -> List[List[float]]:
    """
    Embed many texts in batches — a 300-chunk book used to make 300 separate
    Voyage calls; now a handful. Uses voyage-3-lite, the most cost-effective
    model. Raises EmbeddingError on failure when a key is configured.
    """
    client = _get_voyage_client()
    if client is None:
        return [_mock_embedding(t) for t in texts]
    try:
        out: List[List[float]] = []
        for i in range(0, len(texts), VOYAGE_BATCH):
            result = _embed_batch_with_backoff(client, texts[i:i + VOYAGE_BATCH], "document")
            out.extend(result.embeddings)
        return out
    except Exception as e:
        raise EmbeddingError(f"Voyage AI embedding failed: {e}") from e


def _get_embedding(text: str) -> List[float]:
    """Single-text convenience wrapper around the batched path."""
    return _get_embeddings([text])[0]


def _get_query_embedding(text: str) -> List[float]:
    """
    Embedding for search queries (uses input_type='query' for better retrieval).
    Runs on a LIVE request (chat, insights) — short retry budget (~6s total)
    on a rate limit, not the multi-minute one used for background ingestion.
    """
    client = _get_voyage_client()
    if client is None:
        return _mock_embedding(text)
    try:
        result = _embed_batch_with_backoff(client, [text], "query", max_attempts=3, base_delay=2)
        return result.embeddings[0]
    except Exception as e:
        raise EmbeddingError(f"Voyage AI query embedding failed: {e}") from e


def _classify_embedder(match) -> str:
    """'voyage' or 'mock' for a Pinecone match. New vectors carry an explicit
    metadata stamp; legacy ones are classified by norm — Voyage embeddings are
    unit-length (‖v‖ ≈ 1) while the mock's uniform(-1,1) gives ‖v‖ ≈ 13."""
    meta = match.metadata or {}
    stamped = meta.get("embedder")
    if stamped:
        return stamped
    values = getattr(match, "values", None) or []
    if not values:
        return "voyage"  # can't tell without values — assume real
    norm = math.sqrt(sum(v * v for v in values))
    return "voyage" if norm < 2.0 else "mock"


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
        provider = embedding_provider()
        vectors = []
        for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            vectors.append({
                "id": f"{item_id}_{i}",
                "values": embedding,
                "metadata": {
                    "user_id": user_id,
                    "item_id": item_id,
                    "chunk_index": i,
                    # Lets Connect refuse to score against dev-mock vectors
                    # instead of reporting a nonsense ~4% goal match.
                    "embedder": provider,
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
        """Like search_item but returns [{text, score, embedder}] — scores
        drive the Connect goal-match; embedder lets the caller refuse to score
        against dev-mock vectors (random values → cosine ≈ 0 → nonsense %)."""
        if not self.pinecone_available:
            return []

        query_embedding = _get_query_embedding(query)
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=user_id,
            filter={"item_id": {"$eq": item_id}},
            include_metadata=True,
            include_values=True,
        )
        return [
            {
                "text": (m.metadata or {}).get("text", ""),
                "score": float(m.score or 0),
                "embedder": _classify_embedder(m),
            }
            for m in results.matches
        ]

    # Pinecone metadata filters have a size cap — cap the $nin list and, once a
    # book is nearly fully served, let retrieval recycle earlier chunks.
    MAX_EXCLUDE = 400

    def search_item_fresh(
        self,
        query: str,
        user_id: str,
        item_id: str,
        top_k: int = 8,
        exclude_indexes: Optional[List[int]] = None,
    ):
        """Search within ONE item, skipping already-served chunks, returning
        [{text, chunk_index}]. This is what makes daily sessions cover NEW
        ground instead of re-serving the same top-K forever — and what makes
        the Connect 'Explored %' an honest number instead of an extrapolation."""
        if not self.pinecone_available:
            return []

        flt = {"item_id": {"$eq": item_id}}
        exclude = list(exclude_indexes or [])
        if exclude:
            flt["chunk_index"] = {"$nin": exclude[-self.MAX_EXCLUDE:]}

        query_embedding = _get_query_embedding(query)
        results = self.index.query(
            vector=query_embedding,
            top_k=top_k,
            namespace=user_id,
            filter=flt,
            include_metadata=True,
        )
        out = [
            {
                "text": (m.metadata or {}).get("text", ""),
                "chunk_index": (m.metadata or {}).get("chunk_index"),
            }
            for m in results.matches
        ]
        # Book exhausted under exclusions → recycle from the whole book so the
        # user always gets a session (revisiting > nothing).
        if not out and exclude:
            results = self.index.query(
                vector=query_embedding,
                top_k=top_k,
                namespace=user_id,
                filter={"item_id": {"$eq": item_id}},
                include_metadata=True,
            )
            out = [
                {
                    "text": (m.metadata or {}).get("text", ""),
                    "chunk_index": (m.metadata or {}).get("chunk_index"),
                }
                for m in results.matches
            ]
        return out

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
