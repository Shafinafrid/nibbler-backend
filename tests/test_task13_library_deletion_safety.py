"""
Task 13 — Make Library deletion server-confirmed before erasing local
information. Backend half: the permanent hard-delete tombstone
(`DeletedLibraryItem`) that closes the "outbox can resurrect a deleted
source's data" gap found while auditing the existing deletion contract.

Background: `_book_lock_status`'s `item is None` branch has always meant
"default-allow" (a documented, deliberate policy for legacy/demo book_ids
this table never had an opinion about). That policy is CORRECT for a
book_id that never existed — but once `delete_library_item` fully succeeds
and hard-deletes the row (`_finish_item_deletion_cleanup`), `item is None`
becomes true for THAT book_id too, and the exact same default-allow policy
then lets a queued note/highlight/chat write for the now-gone book through,
even recreating it on a future `/sync/all` restore. `DeletedLibraryItem` is
a permanent, per-user record written in the SAME transaction as the hard
delete, so `item is None` can be told apart from "was deleted."

  A — a full successful delete (real endpoint, S3/Pinecone/images mocked
      to succeed) hard-deletes the row AND writes the tombstone.
  B — a sync write against that now-hard-deleted book_id is rejected
      (403 source_locked), not default-allowed.
  C — a stray row already sitting in the DB with that book_id is excluded
      from GET /sync/all restore, not returned as "unattributed."
  D — regression guard: a book_id that never had any LibraryItem row at
      all is still default-allowed — the fix must not over-broaden.
  E — a PARTIAL failure (S3 fails) leaves the row tombstoned but NOT hard-
      deleted, and writes NO DeletedLibraryItem row yet — the pre-existing
      deletion_state protection covers this case on its own; the new table
      only matters once the row is truly gone.
  F — retrying DELETE on an already-fully-gone item 404s (the contract the
      mobile client's 404-as-success handling now depends on).
  G — the tombstone is user-scoped: another account is unaffected by a
      book_id that was never theirs to begin with.
"""
import os
import sys
import tempfile
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task13.db", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

from fastapi.testclient import TestClient
from app.database import create_tables, SessionLocal, get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.library import LibraryItem, DeletedLibraryItem
from app.models.user_data import Note
import main

create_tables()
db = SessionLocal()

CURRENT_UID = {"v": None}
def _db(): yield db
def _current_user(): return db.query(User).filter(User.id == CURRENT_UID["v"]).first()
main.app.dependency_overrides[get_db] = _db
main.app.dependency_overrides[get_current_user] = _current_user
c = TestClient(main.app)
def as_user(uid): CURRENT_UID["v"] = uid


def mkuser(uid, **kw):
    kw.setdefault("email", f"{uid}@example.com")
    u = User(id=uid, **kw)
    db.add(u); db.commit()
    return u


def mkitem(iid, uid, **kw):
    kw.setdefault("type", "pdf")
    kw.setdefault("title", iid)
    kw.setdefault("processed", True)
    it = LibraryItem(id=iid, user_id=uid, **kw)
    db.add(it); db.commit()
    return it


def note_payload(book_id, note_id):
    return {"id": note_id, "book_id": book_id, "card_index": 0, "text": "hi",
            "book_title": "t", "book_color": "#000", "card_eyebrow": "e",
            "card_title": "ct", "card_body": "cb"}


# ═══════════════════════════════════════════════════════════════════════
section("A — full successful delete hard-deletes the row and writes the tombstone")
# ═══════════════════════════════════════════════════════════════════════

u1 = mkuser("t13_u1")
mkitem("t13_item_a", "t13_u1", file_url="s3/key/a.pdf", images=None)
as_user("t13_u1")

with mock.patch("app.routers.library.S3Service") as MockS3, \
     mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
    MockS3.return_value.delete_file.return_value = True
    MockEmbed.return_value.delete_item_vectors.return_value = True
    r = c.delete("/library/t13_item_a")

check("DELETE returns 200", r.status_code == 200, f"{r.status_code} {r.text[:150]}")
check("response reports external_cleanup_complete=True", r.json().get("external_cleanup_complete") is True, r.json())

db.expire_all()
gone = db.query(LibraryItem).filter(LibraryItem.id == "t13_item_a").first()
check("LibraryItem row is genuinely hard-deleted", gone is None)

tomb_a = db.query(DeletedLibraryItem).filter(
    DeletedLibraryItem.id == "t13_item_a", DeletedLibraryItem.user_id == "t13_u1",
).first()
check("a DeletedLibraryItem tombstone was written for this user+item", tomb_a is not None)


# ═══════════════════════════════════════════════════════════════════════
section("B — a sync write against the hard-deleted book_id is rejected, not default-allowed")
# ═══════════════════════════════════════════════════════════════════════

r = c.put("/sync/notes", json=note_payload("t13_item_a", "n_after_delete"))
check("PUT /sync/notes on the hard-deleted book_id is refused (403 source_locked) — "
      "THIS is the exact hole the tombstone closes: without it, item-is-None falls "
      "through to the default-allow policy and this would be a 200",
      r.status_code == 403 and r.json().get("detail", {}).get("code") == "source_locked",
      f"{r.status_code} {r.text[:150]}")
check("no Note row was created for the rejected write",
      db.query(Note).filter(Note.id == "n_after_delete").first() is None)


# ═══════════════════════════════════════════════════════════════════════
section("C — a stray row for the hard-deleted book_id is excluded from restore")
# ═══════════════════════════════════════════════════════════════════════

# Simulates a note that reached the server from ANOTHER device's queue (or
# any other path) referencing this now-gone book — must not come back on
# a future reinstall/new-device restore.
db.add(Note(id="n_stray", user_id="t13_u1", book_id="t13_item_a", card_index=2,
            book_title="t", book_color="#000", card_eyebrow="e", card_title="ct",
            card_body="cb", text="should not resurrect"))
db.commit()

r = c.get("/sync/all")
note_ids = {n["id"] for n in r.json()["notes"]}
check("GET /sync/all EXCLUDES a note referencing the hard-deleted book_id",
      "n_stray" not in note_ids, note_ids)


# ═══════════════════════════════════════════════════════════════════════
section("D — regression guard: a book_id that never existed is still default-allowed")
# ═══════════════════════════════════════════════════════════════════════

r = c.put("/sync/notes", json=note_payload("t13_never_existed_demo_book", "n_legacy"))
check("PUT /sync/notes against a book_id with NO LibraryItem and NO tombstone (a "
      "legacy/demo reference) still succeeds under the documented default-allow "
      "policy — the fix must not reject ids the table never had an opinion about",
      r.status_code == 200, f"{r.status_code} {r.text[:150]}")


# ═══════════════════════════════════════════════════════════════════════
section("E — a partial cleanup failure tombstones but does NOT hard-delete or write the new table")
# ═══════════════════════════════════════════════════════════════════════

mkitem("t13_item_e", "t13_u1", file_url="s3/key/e.pdf", images=None)

with mock.patch("app.routers.library.S3Service") as MockS3, \
     mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
    MockS3.return_value.delete_file.return_value = False   # S3 cleanup fails
    MockEmbed.return_value.delete_item_vectors.return_value = True
    r = c.delete("/library/t13_item_e")

check("DELETE still returns 200 (the 'gone from your Library' promise already holds)",
      r.status_code == 200, f"{r.status_code} {r.text[:150]}")
check("response reports external_cleanup_complete=False", r.json().get("external_cleanup_complete") is False, r.json())

db.expire_all()
still_there = db.query(LibraryItem).filter(LibraryItem.id == "t13_item_e").first()
check("the row is NOT hard-deleted — kept tombstoned for retry", still_there is not None)
check("its deletion_state is 'failed', durable and retryable",
      still_there is not None and still_there.deletion_state == "failed",
      still_there.deletion_state if still_there else None)

tomb_e = db.query(DeletedLibraryItem).filter(DeletedLibraryItem.id == "t13_item_e").first()
check("no DeletedLibraryItem tombstone was written yet — the row itself still exists "
      "and already protects writes via its own deletion_state, covered pre-existing "
      "behavior, not this remediation", tomb_e is None)

r = c.put("/sync/notes", json=note_payload("t13_item_e", "n_e"))
check("a write against the still-tombstoned (not yet hard-deleted) item is STILL "
      "refused — via the pre-existing deletion_state-is-not-None branch",
      r.status_code == 403, f"{r.status_code} {r.text[:150]}")


# ═══════════════════════════════════════════════════════════════════════
section("F — retrying DELETE on an already-fully-gone item 404s")
# ═══════════════════════════════════════════════════════════════════════

r = c.delete("/library/t13_item_a")
check("a second DELETE for an item that's already hard-deleted returns 404 — the "
      "contract the mobile client's 404-as-success handling now relies on",
      r.status_code == 404, f"{r.status_code} {r.text[:150]}")


# ═══════════════════════════════════════════════════════════════════════
section("G — the tombstone is user-scoped: cross-account isolation")
# ═══════════════════════════════════════════════════════════════════════

u2 = mkuser("t13_u2")
as_user("t13_u2")
r = c.put("/sync/notes", json=note_payload("t13_item_a", "n_other_account"))
check("a DIFFERENT account referencing the SAME (some other user's deleted) book_id "
      "is default-allowed, not rejected — the tombstone must not leak across "
      "accounts as a blanket 'this id is bad' rule",
      r.status_code == 200, f"{r.status_code} {r.text[:150]}")


print("\n" + "=" * 62)
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: all Task 13 backend deletion-safety checks passed")
sys.exit(0)
