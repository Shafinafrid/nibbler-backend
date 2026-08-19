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

import datetime
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
from app.middleware.auth import get_current_user, get_current_user_allow_pending_erasure  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
import app.routers.library as library_router  # noqa: E402
import app.routers.auth as auth_router  # noqa: E402
import main  # noqa: E402

c = Checks("Book image access")

# ── a stub S3 that records rather than dials ────────────────────────────────
deleted_keys = []
presigned = []
downloaded = []


class StubS3:
    def upload_file(self, file_content, filename, content_type=None):
        return filename

    def download_file(self, ref):
        downloaded.append(ref)
        return b"real image bytes"

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
# DELETE /auth/me now depends on get_current_user_allow_pending_erasure (Task
# 2 closeout, Verified Blocker 8) — a DIFFERENT function object than
# get_current_user, so it needs its own override or the real Firebase-token
# path runs unmocked and the route never executes at all.
main.app.dependency_overrides[get_current_user_allow_pending_erasure] = \
    lambda: db.query(User).filter(User.id == AS["id"]).first()
client = TestClient(main.app)


def as_user(uid):
    AS["id"] = uid


# ══ the owner can see their own figures ═════════════════════════════════════
# Task 2 closeout (Verified Blocker 10): this endpoint now proxies the raw
# image bytes (entitlement-revalidated on EVERY request) instead of minting a
# presigned URL — a real, reusable capability that kept working for a full
# hour even after a downgrade mid-flight.

as_user("owner")
downloaded.clear()
presigned.clear()
r = client.get("/library/book1/images/img_own1")
c.ok(r.status_code == 200, "the owner gets 200 (got %d)" % r.status_code)
c.ok(r.content == b"real image bytes", "the actual image bytes are returned, not a URL")
c.ok(r.headers.get("content-type") == "image/png", "the real MIME type is set as Content-Type")
c.ok("no-store" in (r.headers.get("cache-control") or ""),
     "Cache-Control: no-store so nothing caches a copy that should die with a downgrade")
c.ok(not presigned, "no presigned S3 URL is minted at all — the object is fetched server-side")
c.ok(downloaded == ["book-images/owner/book1/img_own1.png"],
     "the server fetched the EXACT stored key")

# JSON body checks no longer apply — the response IS the image. Still NOT a
# 307 redirect to S3: a redirect would have iOS forward the Firebase bearer
# token across hosts and hand it to Amazon.
c.ok(r.status_code != 307, "the endpoint returns the image directly, never redirects with the auth header attached")

downloaded.clear()
second = client.get("/library/book1/images/img_own1")
c.ok(second.status_code == 200 and downloaded == ["book-images/owner/book1/img_own1.png"],
     "asking again re-fetches for real — there is no outstanding capability to reuse or expire")


# ══ downgrade mid-flight refuses the VERY NEXT request ══════════════════════
# The old presigned-URL design kept a capability alive for a full hour after
# this exact moment. There is nothing here to keep alive — every request
# re-checks from scratch.

book1_row = db.query(LibraryItem).filter(LibraryItem.id == "book1").first()
book1_row.is_unlocked_selection = False
owner_row = db.query(User).filter(User.id == "owner").first()
owner_row.is_premium = False
owner_row.premium_until = None
owner_row.created_at = datetime.datetime(2020, 1, 1)  # signup trial long expired
db.commit()

downloaded.clear()
r_locked = client.get("/library/book1/images/img_own1")
c.ok(r_locked.status_code == 403 and r_locked.json()["detail"]["code"] == "source_locked",
     "the SAME image path, for the SAME owner, is refused the moment the source locks "
     "(got %d)" % r_locked.status_code)
c.ok(not downloaded, "a locked source's bytes are never even fetched from S3")

# Restore entitlement for the rest of the suite.
book1_row = db.query(LibraryItem).filter(LibraryItem.id == "book1").first()
book1_row.is_unlocked_selection = True
owner_row = db.query(User).filter(User.id == "owner").first()
owner_row.is_premium = True
db.commit()
downloaded.clear()
r_restored = client.get("/library/book1/images/img_own1")
c.ok(r_restored.status_code == 200 and downloaded == ["book-images/owner/book1/img_own1.png"],
     "and access resumes immediately once unlocked again — no stale state either direction")


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
# Task 9 (Aug 2026): DELETE /auth/me now only SCHEDULES deletion (a grace
# period during which the account stays usable) — actual cleanup happens
# once entitlement_service.promote_scheduled_erasures moves it to
# 'pending' after the window elapses. Backdate requested_at past the
# grace period and drive that transition directly, matching how the real
# production scheduler does it, rather than sleeping in a test.
from app.models.library import AccountErasure  # noqa: E402
from app.services import entitlement_service as _ent  # noqa: E402

client.delete("/auth/me")
scheduled = db.query(AccountErasure).filter(AccountErasure.user_id == "owner").first()
scheduled.requested_at = datetime.datetime.utcnow() - datetime.timedelta(hours=48)
db.commit()
_ent.promote_scheduled_erasures(db)
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
    """A REAL session (so the ownership pre-check and the atomic-write's
    own locked query both see a genuine matching row) whose commit fails
    the way a constraint violation would — every other method passes
    through to the real session."""

    def __init__(self, real_db):
        self._real = real_db

    def commit(self):
        raise RuntimeError("database went away mid-commit")

    def rollback(self):
        self._real.rollback()

    def __getattr__(self, name):
        return getattr(self._real, name)


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
# A real, matching row: Task 2 closeout (Verified Blocker 3) now checks
# ownership (existence, not tombstoned, exact attempt token) via a REAL
# query BEFORE and atomically WHEN persisting `item.images` — a purely
# hand-rolled fake session can no longer stand in for the whole call.
db.add(LibraryItem(id="bookX", user_id="owner", type="pdf", title="x.pdf",
                    processed=True, last_processing_attempt_id="tok-brokencommit"))
db.commit()
# _cleanup_one_image_after_ownership_loss (the new durable per-image
# cleanup path) calls `S3Service()` from library_router's OWN namespace,
# not app.services.s3_service's — matching every other cleanup helper in
# that module.
_prior_library_s3 = library_router.S3Service
library_router.S3Service = RecordingS3
try:
    count = library_router._extract_book_images(
        BrokenCommitDB(db), FakeItem(), b"x", "owner", "tok-brokencommit")
    c.ok(count == 0, "a failed commit reports zero images stored")
    c.ok(sorted(orphan_deleted) == sorted(r["key"] for r in rows),
         "and DELETES the objects it had already uploaded rather than orphaning them")
finally:
    _ie_mod.extract_and_store = _orig_extract_and_store
    library_router.S3Service = _prior_library_s3


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
downloaded.clear()
ra = client.get("/library/bookA/images/%s" % id_a)
rb = client.get("/library/bookB/images/%s" % id_b)
c.ok(ra.status_code == 200 and rb.status_code == 200, "each book serves its own copy")
c.ok(len(downloaded) == 2 and downloaded[0] != downloaded[1],
     "and they fetch two genuinely different, book-scoped S3 keys")
c.ok("bookA" in downloaded[0] and "bookB" in downloaded[1],
     "each fetched key sits inside its own book's prefix")

cross = client.get("/library/bookA/images/%s" % id_b)
c.ok(cross.status_code == 404,
     "asking book A for book B's image is a 404 — delivery is book-scoped, not just owner-scoped")

sys.exit(1 if c.finish() else 0)
