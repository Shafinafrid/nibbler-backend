"""F15: unknown Pinecone namespaces are detected without unsafe deletion."""

import hashlib

import hermetic  # noqa: F401

from app.database import Base, create_engine, sessionmaker
from app.models.library import AccountErasure  # noqa: F401
from app.models.user import User
from app.services.vector_integrity_service import inspect_namespace_ownership


engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db = Session()

db.add(User(id="current-user", email="current@example.invalid"))
db.add(AccountErasure(
    id="erasure-1",
    user_id="deleting-user",
    state="failed",
    identity={"pinecone_namespace": "deleting-user"},
))
db.commit()


class FakeIndex:
    def describe_index_stats(self):
        return {"namespaces": {
            "current-user": {"vector_count": 10},
            "deleting-user": {"vector_count": 20},
            "unknown-user": {"vector_count": 31},
        }}


class FakeEmbedding:
    index = FakeIndex()


result = inspect_namespace_ownership(db, embedding_service=FakeEmbedding())
expected_fingerprint = hashlib.sha256(b"unknown-user").hexdigest()[:12]

checks = {
    "only the genuinely unowned namespace is reported": result["unknown_count"] == 1,
    "current and erasure-owned vectors are excluded": result["unknown_vectors"] == 31,
    "the monitor emits only a one-way fingerprint": result["unknown_fingerprints"] == [expected_fingerprint],
    "the raw namespace is absent from the result": "unknown-user" not in repr(result),
}

failed = []
for label, ok in checks.items():
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        failed.append(label)

db.close()
if failed:
    print(f"RESULT: {len(failed)} FAILURE(S): {failed}")
    raise SystemExit(1)
print("RESULT: ALL PASS")
