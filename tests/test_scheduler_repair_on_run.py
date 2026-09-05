"""
Growth profiles post-audit fix (Sep 2026) — the scheduler's own
_prepare_user_nibbles tick now runs the bootstrap-attach/legacy-promotion
repair passes, matching library.py's list_library fix for the same gap
(plan §1.4/§1.5: "and on later library reads and scheduler runs").

Before this fix, notification_service.py had NO call to
attach_unassigned_wisdom_books/promote_resolvable_legacy_rows/
redetermine_assignment_names anywhere — a bootstrap-unassigned or
now-uniquely-matchable legacy row for a source the scheduler selects for
today's generation only ever self-healed via a growth push the device
happened to make, never simply by the scheduler ticking for that user.

Real DB (SQLite, this repo's standard for non-concurrency suites),
generate_session_for_item stubbed to a no-op so this test exercises ONLY
the repair wiring, not real generation/LLM calls.

    .venv/bin/python tests/test_scheduler_repair_on_run.py
"""
import os
import sys
import tempfile
import uuid
import datetime

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL="sqlite:///%s/scheduler_repair.db" % TMP, FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)

import hermetic  # noqa: E402,F401

from app.database import create_tables, SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.profile import Profile  # noqa: E402
import app.services.notification_service as notif_service  # noqa: E402
import app.services.session_service as session_service  # noqa: E402

failures = []


def check(name, cond, detail=""):
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  — " + str(detail)) if detail else ""))
    if not cond:
        failures.append(name)


def section(t):
    print("\n=== %s ===" % t)


create_tables()

# Stub generation entirely — this test is about the repair pass wiring
# BEFORE generation runs, not about generation itself.
def _fake_generate_session_for_item(*args, **kwargs):
    return None
session_service.generate_session_for_item = _fake_generate_session_for_item
notif_service.generate_session_for_item = _fake_generate_session_for_item


def db_factory():
    return SessionLocal()


def item(item_id):
    db = SessionLocal()
    try:
        return db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
    finally:
        db.close()


section("The scheduler's own tick repairs bootstrap/legacy rows before generation")

uid = "sched-repair-" + uuid.uuid4().hex[:8]
db = SessionLocal()
db.add(User(id=uid, email="%s@example.com" % uid,
            premium_until=datetime.datetime.utcnow() + datetime.timedelta(days=30)))
db.commit()

# Seed the profile row DIRECTLY — no push/ensure call at all, so the ONLY
# thing that can repair the rows below is the scheduler tick itself.
db.add(Profile(
    id="sched-repair-profile", user_id=uid, name="P",
    growth_state={
        "person": {"name": "P"},
        "profiles": [{"id": "A", "name": "Money", "profileName": "Money"}],
        "activeProfileId": "A",
    },
))

boot_id = "sched-boot-" + uuid.uuid4().hex[:8]
legacy_id = "sched-legacy-" + uuid.uuid4().hex[:8]
db.add(LibraryItem(
    id=boot_id, user_id=uid, title="Bootstrap Book", type="text",
    mode="wisdom", processed=True, is_active=True,
    content="A distinctive passage. " * 40,
))
db.add(LibraryItem(
    id=legacy_id, user_id=uid, title="Legacy Book", type="text",
    mode="wisdom", processed=True, is_active=True,
    content="Another distinctive passage. " * 40,
    growth_profile_id=None, growth_profile_name="Money",  # uniquely matchable once repaired
))
db.commit()
db.close()

notif_service._prepare_user_nibbles(db_factory, uid)

check("the bootstrap-unassigned row is attached by the SCHEDULER TICK itself, "
      "with no prior growth push/ensure call",
      item(boot_id).growth_profile_id == "A", str(item(boot_id).growth_profile_id if item(boot_id) else None))
check("the uniquely-matchable legacy row is promoted by the scheduler tick itself",
      item(legacy_id).growth_profile_id == "A",
      str(item(legacy_id).growth_profile_id if item(legacy_id) else None))


print("\n" + "=" * 70)
if failures:
    print("FAILED (%d): %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("ALL CHECKS PASSED")
sys.exit(0)
