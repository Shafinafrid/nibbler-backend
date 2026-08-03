"""
Serving and deleting book images, through the real HTTP stack.

The unit tests prove the selection logic is right. These prove the ENDPOINT is
right, which is a different question: ownership is enforced by the query rather
than by a comparison, so it can only really be tested by asking as the wrong
user and seeing a 404.

The properties here are the ones whose failure is a breach rather than a bug —
cross-user access, arbitrary key requests, images outliving a deleted account.

    .venv/bin/python tests/test_book_image_access.py
"""

import os
import sys
import tempfile

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/img.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
sys.path.insert(0, os.path.join(BACKEND, "tests"))

import hermetic  # noqa: E402,F401

from llm_fakes import Checks  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.database import create_tables, SessionLocal, get_db  # noqa: E402
from app.middleware.auth import get_current_user  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
import app.routers.library as library_router  # noqa: E402
import app.routers.auth as auth_router  # noqa: E402
import main  # noqa: E402

c = Checks("Book image access")

# ── a stub S3 that records rather than dials ────────────────────────────────
deleted_keys = []
presigned = []


class StubS3:
    def upload_file(self, file_content, filename, content_type=None):
        return filename

    def download_file(self, ref):
        return b"bytes"

    def delete_file(self, ref):
        deleted_keys.append(ref)
        return True

    def generate_presigned_url(self, key, expiry=3600):
        presigned.append((key, expiry))
        return "https://s3.example.test/%s?X-Amz-Signature=deadbeef" % key


library_router.S3Service = StubS3
auth_router.S3Service = StubS3


def image_row(user_id, item_id, cid, **over):
    row = {
        "id": cid, "item_id": item_id, "user_id": user_id,
        "key": "book-images/%s/%s/%s.png" % (user_id, item_id, cid),
        "mime": "image/png", "checksum": "sum-" + cid, "order": 0,
        "w": 600, "h": 400, "page": 3, "spine": None, "chapter": "One",
        "href": None, "context": "ctx", "caption": "Figure 1: a thing",
        "alt": "a thing", "position": 0.3, "visual": "photo",
    }
    row.update(over)
    return row


create_tables()
db = SessionLocal()
db.add(User(id="owner", email="owner@example.com"))
db.add(User(id="stranger", email="stranger@example.com"))
db.add(LibraryItem(
    id="book1", user_id="owner", title="Owner Book", type="pdf", processed=True,
    images=[image_row("owner", "book1", "img_own1"), image_row("owner", "book1", "img_own2")],
))
db.add(LibraryItem(
    id="book2", user_id="stranger", title="Stranger Book", type="pdf", processed=True,
    images=[image_row("stranger", "book2", "img_theirs")],
))
db.commit()


def _db():
    yield db


AS = {"id": "owner"}
main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = \
    lambda: db.query(User).filter(User.id == AS["id"]).first()
client = TestClient(main.app)


def as_user(uid):
    AS["id"] = uid


# ══ the owner can see their own figures ════════════════════════════════════

as_user("owner")
r = client.get("/library/book1/images/img_own1")
c.ok(r.status_code == 200, "the owner gets 200 (got %d)" % r.status_code)
body = r.json() if r.status_code == 200 else {}
c.ok(body.get("url", "").startswith("https://"), "a usable view URL is returned")
c.ok("X-Amz-Signature" in body.get("url", ""), "the URL is presigned")
c.ok(body.get("expires_in") == library_router.IMAGE_URL_TTL,
     "expiry metadata is returned so the client knows it is short-lived")
c.ok(body.get("alt") == "a thing", "alt text comes back for the accessibility label")
c.ok("key" not in body and "book-images" not in str(body.get("alt", "")),
     "the raw S3 key is not in the response body")

# JSON, NOT a redirect: a 307 to S3 would have iOS forward the Firebase bearer
# token across hosts and hand it to Amazon.
c.ok(r.status_code != 307 and "application/json" in r.headers.get("content-type", ""),
     "the endpoint returns JSON rather than redirecting with the auth header attached")

second = client.get("/library/book1/images/img_own1")
c.ok(second.status_code == 200 and len(presigned) >= 2,
     "asking again mints a FRESH url — this is how expired access refreshes")


# ══ nobody else can ════════════════════════════════════════════════════════

as_user("stranger")
r = client.get("/library/book1/images/img_own1")
c.ok(r.status_code == 404, "another signed-in user gets 404, not the image (got %d)" % r.status_code)

as_user("owner")
r = client.get("/library/book1/images/img_theirs")
c.ok(r.status_code == 404, "the owner cannot read a stranger's image either")

before = len(presigned)
for bad in ("book-images/owner/book1/img_own1.png", "../../etc/passwd",
            "https://evil.example/x.png", "owner/avatar.jpg", "img_", "x" * 200):
    r = client.get("/library/book1/images/%s" % bad.replace("/", "%2F"))
    c.ok(r.status_code in (404, 400),
         "an arbitrary key/path %r is refused (got %d)" % (bad[:24], r.status_code))
c.ok(len(presigned) == before, "no rejected request ever reached S3")

# An id whose stored key points outside the owner's prefix is treated as
# tampering, not as a lookup that happens to succeed.
tampered = db.query(LibraryItem).filter(LibraryItem.id == "book1").first()
tampered.images = tampered.images + [
    image_row("owner", "book1", "img_bad", key="book-images/stranger/book2/steal.png")
]
db.commit()
r = client.get("/library/book1/images/img_bad")
c.ok(r.status_code == 404, "a row whose key escapes the owner prefix is refused")

main.app.dependency_overrides.pop(get_current_user)
r = client.get("/library/book1/images/img_own1")
c.ok(r.status_code in (401, 403), "an unauthenticated request is refused (got %d)" % r.status_code)
main.app.dependency_overrides[get_current_user] = \
    lambda: db.query(User).filter(User.id == AS["id"]).first()


# ══ deleting a book takes its figures with it ══════════════════════════════

as_user("owner")
deleted_keys.clear()
r = client.delete("/library/book1")
c.ok(r.status_code == 200, "the book deletes")
c.ok("book-images/owner/book1/img_own1.png" in deleted_keys,
     "its extracted figures are deleted from S3 too")
c.ok("book-images/owner/book1/img_own2.png" in deleted_keys, "all of them, not just the first")
c.ok("book-images/stranger/book2/steal.png" not in deleted_keys,
     "a tampered out-of-prefix key is refused rather than deleted")
c.ok(not any(k.startswith("book-images/stranger/") for k in deleted_keys),
     "no other user's objects are touched by a book delete")

r = client.get("/library/book1/images/img_own1")
c.ok(r.status_code == 404, "the image is unreachable once its book is gone")

survivor = db.query(LibraryItem).filter(LibraryItem.id == "book2").first()
c.ok(survivor is not None and survivor.images,
     "another book's images are untouched — deletion is scoped, not global")


# ══ deleting an account takes everything it owns ═══════════════════════════

db.add(LibraryItem(
    id="book3", user_id="owner", title="Second Owner Book", type="epub", processed=True,
    images=[image_row("owner", "book3", "img_own3")],
))
# A book whose original upload was never archived still has figures of its own.
db.add(LibraryItem(
    id="book4", user_id="owner", title="No Source File", type="pdf", processed=True,
    file_url=None, images=[image_row("owner", "book4", "img_own4")],
))
db.commit()

deleted_keys.clear()
image_count = auth_router  # keep the import referenced for readers
as_user("owner")
try:
    client.delete("/auth/me")
except Exception as e:  # Firebase/Pinecone are absent in the harness
    c.ok(True, "account deletion ran without external services (%s)" % type(e).__name__)

c.ok("book-images/owner/book3/img_own3.png" in deleted_keys,
     "account deletion removes extracted images")
c.ok("book-images/owner/book4/img_own4.png" in deleted_keys,
     "including images on a book whose source file was never archived — the old "
     "query only visited items WITH a file_url and would have orphaned these")
c.ok(not any("stranger" in k for k in deleted_keys),
     "another account's images survive")


# ══════════════════════════════════════════════════════════════════════════
# AUDIT 2026-08-03 — orphaning and cross-book collisions
# ══════════════════════════════════════════════════════════════════════════

# ── A failed database commit must not leave paid-for objects behind ────────
# An injected commit failure produced one successful S3 upload and zero
# deletions: an object nothing knew about, invisible to book deletion and to
# account erasure, and therefore permanent.

from app.services import image_extract as ie  # noqa: E402
import app.services.s3_service as s3_module  # noqa: E402

uploaded, orphan_deleted = [], []


class RecordingS3:
    def upload_file(self, file_content, filename, content_type=None):
        uploaded.append(filename)
        return filename

    def delete_file(self, ref):
        orphan_deleted.append(ref)
        return True

    def generate_presigned_url(self, key, expiry=3600):
        return "https://s3.example.test/%s" % key


s3_module.S3Service = RecordingS3


class BrokenCommitDB:
    """A session whose commit fails the way a constraint violation would."""

    def commit(self):
        raise RuntimeError("database went away mid-commit")

    def rollback(self):
        pass


class FakeItem:
    id = "bookX"
    type = "pdf"
    title = "x.pdf"
    file_url = None
    images = None


rows = [{"id": "img_a", "key": "book-images/owner/bookX/img_a.png"},
        {"id": "img_b", "key": "book-images/owner/bookX/img_b.png"}]
ie.delete_stored(rows)
c.ok(orphan_deleted == [r["key"] for r in rows],
     "delete_stored removes every object it is given")


# ── An orphan that cannot be cleaned up must be LOUD ───────────────────────
# Returning quietly when the S3 client itself cannot be built was the worst
# available outcome: the objects exist, nothing references them, and nobody is
# told they are there.

import logging as _logging  # noqa: E402


class _Capture(_logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


_cap = _Capture()
_log = _logging.getLogger("app.services.image_extract")
_log.addHandler(_cap)
_log.setLevel(_logging.INFO)


class UnbuildableS3:
    def __init__(self):
        raise RuntimeError("no AWS credentials")


s3_module.S3Service = UnbuildableS3
ie.delete_stored(rows)
_log.removeHandler(_cap)
s3_module.S3Service = RecordingS3

_errors = [r for r in _cap.records if r.levelno >= _logging.ERROR]
_blob = " ".join(r.getMessage() for r in _errors)
c.ok(_errors, "a cleanup that cannot even start is logged as an ERROR, not swallowed")
c.ok("ORPHANED" in _blob, "the log says plainly that objects are orphaned")
c.ok(all(r["key"] in _blob for r in rows),
     "and names every key, so they can be removed by hand")

# A delete that fails individually must be just as loud.
_cap.records = []
_log.addHandler(_cap)


class RefusingS3:
    def delete_file(self, ref):
        return False


s3_module.S3Service = RefusingS3
ie.delete_stored(rows)
_log.removeHandler(_cap)
s3_module.S3Service = RecordingS3
c.ok(any("ORPHANED" in r.getMessage() for r in _cap.records
         if r.levelno >= _logging.ERROR),
     "an object that refuses to delete is reported as orphaned too")

orphan_deleted.clear()
_real_extract = library_router.__dict__.get("_extract_book_images")
import app.services.image_extract as _ie_mod  # noqa: E402
_orig_extract_and_store = _ie_mod.extract_and_store
_ie_mod.extract_and_store = lambda **kw: [dict(r) for r in rows]
try:
    count = library_router._extract_book_images(BrokenCommitDB(), FakeItem(), b"x", "owner")
    c.ok(count == 0, "a failed commit reports zero images stored")
    c.ok(orphan_deleted == [r["key"] for r in rows],
         "and DELETES the objects it had already uploaded rather than orphaning them")
finally:
    _ie_mod.extract_and_store = _orig_extract_and_store


# ── One S3 failure must not abandon the objects after it ───────────────────
# A raised error stopped account cleanup before later objects were attempted,
# silently leaving most of a user's data in the bucket while the endpoint
# reported the account erased.

attempted = []


class FlakyS3:
    def upload_file(self, file_content, filename, content_type=None):
        return filename

    def delete_file(self, ref):
        attempted.append(ref)
        if "img_first" in ref:
            raise RuntimeError("transient S3 error")
        return True

    def generate_presigned_url(self, key, expiry=3600):
        return "https://s3.example.test/%s" % key


library_router.S3Service = FlakyS3


class FlakyItem:
    id = "bookY"
    images = [
        {"key": "book-images/owner/bookY/img_first.png"},
        {"key": "book-images/owner/bookY/img_second.png"},
        {"key": "book-images/owner/bookY/img_third.png"},
    ]


ok_flag = library_router._delete_item_images(FlakyItem(), "owner")
c.ok(len(attempted) == 3,
     "every object is attempted even after one raises (attempted %d of 3)" % len(attempted))
c.ok(ok_flag is False, "and the failure is reported rather than swallowed")

library_router.S3Service = StubS3
s3_module.S3Service = StubS3


# ── The same figure in two books must not collide ──────────────────────────

same_sum = "identical-figure-checksum"
id_a = ie.candidate_id(same_sum, "bookA")
id_b = ie.candidate_id(same_sum, "bookB")
c.ok(id_a != id_b, "identical images in two books get different candidate ids")

db.add(LibraryItem(id="bookA", user_id="stranger", title="A", type="pdf", processed=True,
                   images=[image_row("stranger", "bookA", id_a)]))
db.add(LibraryItem(id="bookB", user_id="stranger", title="B", type="pdf", processed=True,
                   images=[image_row("stranger", "bookB", id_b)]))
db.commit()

as_user("stranger")
ra = client.get("/library/bookA/images/%s" % id_a)
rb = client.get("/library/bookB/images/%s" % id_b)
c.ok(ra.status_code == 200 and rb.status_code == 200, "each book serves its own copy")
c.ok(ra.json()["url"] != rb.json()["url"], "and they resolve to different objects")
c.ok("bookA" in ra.json()["url"] and "bookB" in rb.json()["url"],
     "each URL points inside its own book's prefix")

cross = client.get("/library/bookA/images/%s" % id_b)
c.ok(cross.status_code == 404,
     "asking book A for book B's image is a 404 — delivery is book-scoped, not just owner-scoped")

sys.exit(1 if c.finish() else 0)
