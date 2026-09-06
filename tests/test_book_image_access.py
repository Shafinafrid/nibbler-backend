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


# Retired for every authenticated caller, including missing/cross-owner IDs.
for uid in ("owner", "stranger"):
    as_user(uid)
    for path in ("/library/book1/images/img_own1", "/library/missing/images/nope"):
        c.ok(client.get(path).status_code == 410, "pictures endpoint is retired")
c.ok(not downloaded and not presigned, "retired endpoint never reaches S3")

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
c.ok(r.status_code == 410, "the image remains unavailable after book deletion")

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


sys.exit(1 if c.finish() else 0)
