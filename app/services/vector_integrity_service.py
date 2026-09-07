"""Privacy-safe ownership reconciliation for Pinecone namespaces.

The account-erasure state machine already retains a deletion identity until
Pinecone confirms that the namespace is gone. This monitor closes the other
half of the operational contract: it detects namespaces that are owned by
neither a current user nor an in-flight erasure. It never deletes an unknown
namespace automatically and never emits raw user ids or namespace names.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from app.models.library import AccountErasure
from app.models.user import User
from app.services.embedding_service import EmbeddingService


def _vector_count(value: Any) -> int:
    if isinstance(value, dict):
        value = value.get("vector_count", 0)
    else:
        value = getattr(value, "vector_count", 0)
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def inspect_namespace_ownership(
    db: Session,
    *,
    embedding_service: EmbeddingService | None = None,
) -> dict:
    """Return aggregate orphan metadata without exposing account identity.

    A namespace referenced by an unresolved/resolved ``AccountErasure`` is
    deliberately treated as owned: its durable retry record is still the
    authority responsible for deleting it. Everything else absent from
    ``users`` is anomalous and requires a human provenance review before any
    destructive action.
    """
    service = embedding_service or EmbeddingService()
    stats = service.index.describe_index_stats()
    namespaces = (
        stats.get("namespaces", {}) if isinstance(stats, dict)
        else getattr(stats, "namespaces", {})
    ) or {}

    owned = {str(uid) for (uid,) in db.query(User.id).all() if uid}
    for identity, in db.query(AccountErasure.identity).all():
        namespace = (identity or {}).get("pinecone_namespace")
        if namespace:
            owned.add(str(namespace))

    unknown = []
    for namespace, metadata in namespaces.items():
        namespace = str(namespace)
        if namespace not in owned:
            unknown.append({
                "fingerprint": hashlib.sha256(namespace.encode("utf-8")).hexdigest()[:12],
                "vectors": _vector_count(metadata),
            })

    unknown.sort(key=lambda row: row["fingerprint"])
    return {
        "namespace_count": len(namespaces),
        "unknown_count": len(unknown),
        "unknown_vectors": sum(row["vectors"] for row in unknown),
        "unknown_fingerprints": [row["fingerprint"] for row in unknown],
    }
