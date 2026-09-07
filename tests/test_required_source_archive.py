"""Regression coverage for the fail-closed original-file archive contract."""
import hashlib
from datetime import datetime, timedelta
from unittest import mock

import hermetic  # noqa: F401 -- must precede app imports
from sqlalchemy.exc import IntegrityError

from app.database import create_tables, SessionLocal
from app.models.library import LibraryItem
from app.models.user import User
from app.routers import library
from app.services import entitlement_service
from app.services.s3_service import S3Service


create_tables()
db = SessionLocal()


def make_user(uid, *, premium=True):
    old = datetime.utcnow() - timedelta(days=60)
    user = User(
        id=uid,
        email=f"{uid}@example.test",
        is_premium=premium,
        created_at=old,
        trial_anchor_at=old,
    )
    db.add(user)
    db.commit()
    return user


def make_file(item_id, user_id, kind="pdf", **overrides):
    values = dict(
        id=item_id,
        user_id=user_id,
        title=item_id,
        type=kind,
        file_size=123,
        archive_required=True,
        processed=False,
    )
    values.update(overrides)
    item = LibraryItem(**values)
    db.add(item)
    db.commit()
    return item


class _FailingArchive:
    deletes = []

    def upload_file(self, **_kwargs):
        raise RuntimeError("S3 unavailable")

    def delete_file(self, key):
        self.deletes.append(key)
        return True


for kind, processor, extract_target in (
    ("pdf", library.process_pdf_embeddings, "app.services.text_extract.pdf_to_structured_text"),
    ("epub", library.process_epub_embeddings, "app.routers.library._extract_epub_text"),
):
    uid = f"archive-{kind}"
    item_id = f"archive-{kind}-item"
    make_user(uid, premium=(kind == "epub"))
    make_file(item_id, uid, kind=kind)
    _FailingArchive.deletes = []
    with mock.patch("app.routers.library.S3Service", _FailingArchive), \
         mock.patch(extract_target) as extract, \
         mock.patch("app.routers.library.EmbeddingService") as embeddings, \
         mock.patch("time.sleep", return_value=None):
        processor(item_id, b"source-bytes", uid)

    db.expire_all()
    failed = db.get(LibraryItem, item_id)
    assert failed.processed is False
    assert failed.archive_status == "failed"
    assert failed.file_url is None
    assert failed.processing_error == library.ARCHIVE_DOWN_MESSAGE
    assert extract.call_count == 0, f"{kind} extraction ran without an archive"
    assert embeddings.call_count == 0, f"{kind} indexing ran without an archive"
    assert len(_FailingArchive.deletes) == 1, "uncertain S3 key was not cleaned"
    if kind == "pdf":
        assert failed.entitlement_status == "released"
        db.expire_all()
        assert db.get(User, uid).reserved_sources_count == 0


# The application-level finalizer independently refuses an unarchived file.
make_user("finalize-user")
unarchived = make_file(
    "finalize-item", "finalize-user",
    entitlement_status="premium", last_processing_attempt_id="attempt-1",
)
assert entitlement_service.finalize_successful_processing(
    db, unarchived, "finalize-user", 7, attempt_token="attempt-1",
) is False
db.expire_all()
assert db.get(LibraryItem, "finalize-item").processed is False


# The database itself refuses the forbidden state, even if code bypasses the
# finalizer and writes through the ORM directly.
forbidden = make_file("constraint-item", "finalize-user")
forbidden.processed = True
try:
    db.commit()
    raise AssertionError("database accepted a processed file with no archive")
except IntegrityError:
    db.rollback()


# The S3 adapter verifies both byte count and a content digest after PUT.
class _S3Client:
    def __init__(self, *, corrupt=False):
        self.kwargs = None
        self.corrupt = corrupt

    def put_object(self, **kwargs):
        self.kwargs = kwargs

    def head_object(self, **_kwargs):
        return {
            "ContentLength": len(self.kwargs["Body"]),
            "Metadata": {
                "sha256": "wrong" if self.corrupt else self.kwargs["Metadata"]["sha256"]
            },
        }


payload = b"exact original bytes"
service = object.__new__(S3Service)
service.bucket = "test-bucket"
service.client = _S3Client()
assert service.upload_file(payload, "u/book/file.pdf", "application/pdf") == "u/book/file.pdf"
assert service.client.kwargs["Metadata"]["sha256"] == hashlib.sha256(payload).hexdigest()

service.client = _S3Client(corrupt=True)
try:
    service.upload_file(payload, "u/book/file.pdf", "application/pdf")
    raise AssertionError("S3 verification accepted mismatched digest metadata")
except RuntimeError as exc:
    assert "verification failed" in str(exc)


db.close()
print("RESULT: required source archive contract PASS")
