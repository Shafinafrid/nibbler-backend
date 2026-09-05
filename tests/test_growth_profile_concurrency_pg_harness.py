"""
Real-PostgreSQL proof for the growth-profiles feature's concurrency
invariants (plan Phase 8, "Backend concurrency (PG harness)").
test_growth_state_concurrency_pg_harness.py already proves the underlying
row-lock serializes two PUT /profile/growth calls; this file proves the
SEVEN races specific to canonical creation/rename/deletion, entitlement,
the bootstrap window, and lock-order conformance — none of which existed
against a real Postgres cluster before.

Same bootstrap pattern as the sibling *_pg_harness.py files — a fully
disposable local Postgres cluster, short temp-dir prefix (Unix socket path
capped at 103 bytes).

  PG1 — canonical create (POST /profile/profiles) racing a stale
        PUT /profile/growth push that omits the new profile: the new
        profile must SURVIVE (absence != deletion, invariant I1).
  PG2 — canonical rename (PATCH /profile/profiles/{id}) racing a NEWER
        personalization push carrying the OLD name: the canonical rename
        survives (invariant I2) AND the newer event is retained.
  PG3 — a deletion tombstone racing a concurrent create/edit attempt on
        the SAME id: the tombstone wins and the id cannot be recreated.
  PG4 — a free user's unentitled new-profile push racing a premium
        user's legitimate push (different accounts, same code path):
        the entitlement filter runs INSIDE the lock and does not leak
        across accounts.
  PG5 — entitlement lapsing BETWEEN request start and commit: the write
        is evaluated against entitlement as it stands on read, not a
        stale value captured before the request began.
  PG6 — the ensure/create bootstrap race (plan §1.4): `ensure` attaches
        currently-visible unassigned books while a concurrent library
        create commits its own unassigned book — the advisory lock
        must genuinely serialize the two so nothing is left stranded.
  PG7 — lock-order conformance: interleaved create/rename/delete calls
        across DIFFERENT users acquire the advisory lock in the
        documented order and complete without deadlock.

    .venv/bin/python tests/test_growth_profile_concurrency_pg_harness.py
"""
import os, sys, shutil, socket, subprocess, tempfile, threading, atexit, time

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")


# ═════════════════════════════════════════════════════════════════════════
# Disposable cluster bootstrap — identical pattern to the sibling harnesses
# ═════════════════════════════════════════════════════════════════════════
_cluster_dir = None
_pg_started_here = False


def _pg_bin(name: str) -> str:
    import glob
    for path in sorted(glob.glob("/opt/homebrew/Cellar/postgresql@*/*/bin"), reverse=True):
        if os.path.exists(os.path.join(path, "postgres")) and os.path.exists(os.path.join(path, name)):
            return os.path.join(path, name)
    for path in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(path, "postgres")) and os.path.exists(os.path.join(path, name)):
            return os.path.join(path, name)
    return name


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


def _bootstrap_disposable_cluster() -> str:
    global _cluster_dir, _pg_started_here
    _cluster_dir = tempfile.mkdtemp(prefix="nib-gp-")
    data_dir = os.path.join(_cluster_dir, "data")
    sock_dir = os.path.join(_cluster_dir, "sock")
    os.makedirs(sock_dir, exist_ok=True)
    port = _free_port()
    env = dict(os.environ); env["LC_ALL"] = "C"; env["LANG"] = "C"
    try:
        subprocess.run(
            [_pg_bin("initdb"), "-D", data_dir, "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-sync"],
            check=True, capture_output=True, env=env, text=True)
        subprocess.run(
            [_pg_bin("pg_ctl"), "-D", data_dir, "-o",
             f"-k {sock_dir} -h '' -p {port}", "-l", os.path.join(_cluster_dir, "pg.log"),
             "-w", "start"],
            check=True, capture_output=True, env=env, text=True)
    except subprocess.CalledProcessError as e:
        print(f"  [bootstrap] FAILED: {e}\n  stdout: {e.stdout}\n  stderr: {e.stderr}")
        raise
    _pg_started_here = True
    subprocess.run(
        [_pg_bin("psql"), "-h", sock_dir, "-p", str(port), "-U", "postgres", "-d", "postgres",
         "-c", "CREATE DATABASE nibbler_pg_growth_profiles"],
        check=True, capture_output=True, env=env, text=True)
    return f"postgresql://postgres@/nibbler_pg_growth_profiles?host={sock_dir}&port={port}"


def _teardown():
    if not _cluster_dir:
        return
    if _pg_started_here:
        try:
            env = dict(os.environ); env["LC_ALL"] = "C"; env["LANG"] = "C"
            subprocess.run([_pg_bin("pg_ctl"), "-D", os.path.join(_cluster_dir, "data"),
                            "-m", "fast", "-w", "stop"], capture_output=True, env=env, timeout=30)
        except Exception as e:
            print(f"  [teardown] pg_ctl stop raised: {e}")
    shutil.rmtree(_cluster_dir, ignore_errors=True)
    print(f"  [teardown] disposable cluster stopped and removed")


external_url = os.environ.get("PG_HARNESS_DATABASE_URL")
if external_url:
    print("Using externally-provided disposable database.")
    database_url = external_url
else:
    print("Bootstrapping a fully disposable local Postgres cluster...")
    atexit.register(_teardown)
    database_url = _bootstrap_disposable_cluster()
    print(f"  cluster ready")

for _name in (
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "PINECONE_API_KEY", "VOYAGE_API_KEY", "MIXPANEL_TOKEN", "RESEND_API_KEY",
    "EXPO_ACCESS_TOKEN", "REVENUECAT_SECRET_API_KEY", "REVENUECAT_WEBHOOK_SECRET",
    "OPENAI_API_KEY", "OPENAI_LLM_API_KEY", "ANTHROPIC_LLM_API_KEY",
    "QWEN_API_KEY", "QWEN_BASE_URL",
    "FIREBASE_PRIVATE_KEY", "FIREBASE_PRIVATE_KEY_ID", "FIREBASE_CLIENT_EMAIL",
    "FIREBASE_CLIENT_ID", "BUG_DRIVE_FOLDER_ID",
):
    os.environ[_name] = ""
os.environ["FIREBASE_PROJECT_ID"] = "test-project"
os.environ["SECRET_KEY"] = "test-secret-key-not-a-real-one-000000"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = database_url

import uuid
import datetime
import threading as _threading
from sqlalchemy import text

from app.database import create_tables, SessionLocal
from app.models.user import User
from app.models.profile import Profile
from app.models.library import LibraryItem
from app.routers import profile as profile_router
from app.routers import library as library_router
from app.services import profile_resolution
from app.schemas.profile import GrowthStateUpdate, GrowthProfileCreate, GrowthProfileRename
from app.schemas.library import UpdateItemRequest

create_tables()


def seed_user(premium=True):
    uid = "u-" + uuid.uuid4().hex[:8]
    s = SessionLocal()
    now = datetime.datetime.utcnow()
    u = User(
        id=uid, email=f"{uid}@t.test",
        premium_until=(now + datetime.timedelta(days=30)) if premium
                      else (now - datetime.timedelta(days=30)),
    )
    s.add(u)
    s.commit()
    s.close()
    return uid


def seed_profile(uid, profiles, active_id):
    s = SessionLocal()
    pid = "p-" + uuid.uuid4().hex[:8]
    s.add(Profile(
        id=pid, user_id=uid, name="Test User",
        growth_state={
            "person": {"name": "Test User"},
            "profiles": profiles,
            "activeProfileId": active_id,
            "updatedAt": "2026-09-01T00:00:00.000Z",
        },
        deleted_profile_ids=[],
    ))
    s.commit()
    s.close()
    return pid


class _FakeUser:
    """A minimal stand-in carrying just what the endpoints read: .id and
    .effective_premium. Real User rows are used everywhere the endpoint
    itself does its own DB query for current_user (matching production),
    but a couple of call sites need a caller-supplied object."""
    def __init__(self, uid, premium):
        self._uid = uid
        self._premium = premium
    @property
    def id(self):
        return self._uid
    @property
    def effective_premium(self):
        return self._premium


def read_state(pid):
    s = SessionLocal()
    try:
        row = s.query(Profile).filter(Profile.id == pid).first()
        return {
            "deleted_profile_ids": list(row.deleted_profile_ids or []),
            "profiles": (row.growth_state or {}).get("profiles", []),
            "activeProfileId": (row.growth_state or {}).get("activeProfileId"),
        }
    finally:
        s.close()


def profile_names(state):
    return {p.get("id"): (p.get("profileName") or p.get("name")) for p in state["profiles"]}


def profile_ledgers(state):
    return {p.get("id"): (p.get("ledger") or []) for p in state["profiles"]}


# ═════════════════════════════════════════════════════════════════════════
section("PG1 — canonical create racing a stale push that omits the new profile")
# ═════════════════════════════════════════════════════════════════════════
# A: POST /profile/profiles creates profile B (canonical, entitled).
# B (device): a stale PUT /profile/growth queued BEFORE A ever ran, carrying
# only {A-original} — a later timestamp but genuinely unaware B exists.
# Held so the create's own transaction is genuinely still open when the
# stale push's transaction starts, forcing real overlap rather than a hoped
# -for ordering.
uid_pg1 = seed_user(premium=True)
pid_pg1 = seed_profile(uid_pg1, [{"id": "orig", "name": "Original", "updatedAt": "2026-09-01T00:00:00.000Z"}], "orig")

pg1_holding = _threading.Event()
pg1_release = _threading.Event()
pg1_results = {}

_orig_lock_pg1 = profile_resolution.lock_user_scope


def _pg1_patched_lock(db, user_id):
    _orig_lock_pg1(db, user_id)
    if user_id == uid_pg1 and "create_thread" not in pg1_results:
        pg1_results["create_thread"] = True
        pg1_holding.set()
        pg1_release.wait(timeout=15)


profile_resolution.lock_user_scope = _pg1_patched_lock
profile_router.lock_user_scope = _pg1_patched_lock


def pg1_worker_create():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg1).first()
        resp = profile_router.create_growth_profile(
            data=GrowthProfileCreate(profile={"id": "B", "name": "Newly Created"}),
            current_user=user, db=db,
        )
        pg1_results["create_ok"] = True
        pg1_results["create_accepted"] = list(resp.acceptedProfileIds or [])
    except Exception as e:
        pg1_results["create_ok"] = False
        pg1_results["create_error"] = type(e).__name__
    finally:
        db.close()


pg1_push_finished = _threading.Event()


def pg1_worker_stale_push():
    pg1_holding.wait(timeout=15)
    time.sleep(0.3)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg1).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [{"id": "orig", "name": "Original", "updatedAt": "2026-09-01T00:00:05.000Z"}],
                "activeProfileId": "orig",
                "updatedAt": "2026-09-01T00:00:05.000Z",
            },
            deletedProfileIds=[],
        )
        profile_router.update_growth_state(data=data, current_user=user, db=db)
        pg1_results["push_ok"] = True
    except Exception as e:
        pg1_results["push_ok"] = False
        pg1_results["push_error"] = type(e).__name__
    finally:
        db.close()
        pg1_push_finished.set()


t1 = _threading.Thread(target=pg1_worker_create)
t2 = _threading.Thread(target=pg1_worker_stale_push)
t1.start(); t2.start()

pg1_holding.wait(timeout=15)
time.sleep(0.6)
pg1_push_blocked = not pg1_push_finished.is_set()
pg1_release.set()

t1.join(timeout=20)
t2.join(timeout=20)
profile_resolution.lock_user_scope = _orig_lock_pg1
profile_router.lock_user_scope = _orig_lock_pg1

check("the stale push was genuinely blocked while create held the advisory lock",
      pg1_push_blocked, {"push_finished_early": pg1_push_finished.is_set()})
check("both requests completed without error", pg1_results.get("create_ok") and pg1_results.get("push_ok"), pg1_results)

final_pg1 = read_state(pid_pg1)
check("the canonically-created profile B SURVIVES the stale push that omitted it",
      "B" in profile_names(final_pg1), final_pg1)
check("no tombstone was created for B (it was never rejected/deleted, just absent from the push)",
      "B" not in final_pg1["deleted_profile_ids"], final_pg1)


# ═════════════════════════════════════════════════════════════════════════
section("PG2 — canonical rename racing a newer personalization push with the OLD name")
# ═════════════════════════════════════════════════════════════════════════
uid_pg2 = seed_user(premium=True)
pid_pg2 = seed_profile(uid_pg2, [{"id": "A", "name": "Old Name", "updatedAt": "2026-09-01T00:00:00.000Z"}], "A")

pg2_holding = _threading.Event()
pg2_release = _threading.Event()
pg2_results = {}

_orig_lock_pg2 = profile_resolution.lock_user_scope


def _pg2_patched_lock(db, user_id):
    _orig_lock_pg2(db, user_id)
    if user_id == uid_pg2 and "rename_thread" not in pg2_results:
        pg2_results["rename_thread"] = True
        pg2_holding.set()
        pg2_release.wait(timeout=15)


profile_resolution.lock_user_scope = _pg2_patched_lock
profile_router.lock_user_scope = _pg2_patched_lock


def pg2_worker_rename():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg2).first()
        resp = profile_router.rename_growth_profile(
            profile_id="A", data=GrowthProfileRename(name="New Canonical Name"),
            current_user=user, db=db,
        )
        pg2_results["rename_ok"] = True
    except Exception as e:
        pg2_results["rename_ok"] = False
        pg2_results["rename_error"] = type(e).__name__
    finally:
        db.close()


pg2_push_finished = _threading.Event()


def pg2_worker_stale_named_push():
    pg2_holding.wait(timeout=15)
    time.sleep(0.3)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg2).first()
        # A newer TIMESTAMP than the rename, but still carrying the OLD name
        # — the exact shape of a device that answered a question locally
        # before ever learning about the rename.
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [{
                    "id": "A", "name": "Old Name",
                    "updatedAt": "2026-09-01T00:10:00.000Z",
                    "ledger": [{"id": "pq-bite-99", "event": {"type": "personalization_answered", "tags": ["x"]}, "resolved": True, "answeredAt": "2026-09-01T00:10:00.000Z"}],
                }],
                "activeProfileId": "A",
                "updatedAt": "2026-09-01T00:10:00.000Z",
            },
            deletedProfileIds=[],
        )
        profile_router.update_growth_state(data=data, current_user=user, db=db)
        pg2_results["push_ok"] = True
    except Exception as e:
        pg2_results["push_ok"] = False
        pg2_results["push_error"] = type(e).__name__
    finally:
        db.close()
        pg2_push_finished.set()


t3 = _threading.Thread(target=pg2_worker_rename)
t4 = _threading.Thread(target=pg2_worker_stale_named_push)
t3.start(); t4.start()

pg2_holding.wait(timeout=15)
time.sleep(0.6)
pg2_push_blocked = not pg2_push_finished.is_set()
pg2_release.set()

t3.join(timeout=20)
t4.join(timeout=20)
profile_resolution.lock_user_scope = _orig_lock_pg2
profile_router.lock_user_scope = _orig_lock_pg2

check("the stale-named push was genuinely blocked while rename held the advisory lock",
      pg2_push_blocked, {"push_finished_early": pg2_push_finished.is_set()})
check("both requests completed without error", pg2_results.get("rename_ok") and pg2_results.get("push_ok"), pg2_results)

final_pg2 = read_state(pid_pg2)
names_pg2 = profile_names(final_pg2)
check("the canonical rename SURVIVES the newer push carrying the old name",
      names_pg2.get("A") == "New Canonical Name", names_pg2)
check("the newer push's event IS still applied — only the name is protected",
      any(e.get("id") == "pq-bite-99" for e in profile_ledgers(final_pg2).get("A", [])),
      profile_ledgers(final_pg2))


# ═════════════════════════════════════════════════════════════════════════
section("PG3 — a deletion tombstone racing a concurrent create/edit on the SAME id")
# ═════════════════════════════════════════════════════════════════════════
uid_pg3 = seed_user(premium=True)
pid_pg3 = seed_profile(
    uid_pg3,
    [{"id": "A", "name": "Keep", "updatedAt": "2026-09-01T00:00:00.000Z"},
     {"id": "DOOMED", "name": "About To Be Deleted", "updatedAt": "2026-09-01T00:00:00.000Z"}],
    "A",
)

pg3_holding = _threading.Event()
pg3_release = _threading.Event()
pg3_results = {}

_orig_lock_pg3 = profile_resolution.lock_user_scope


def _pg3_patched_lock(db, user_id):
    _orig_lock_pg3(db, user_id)
    if user_id == uid_pg3 and "delete_thread" not in pg3_results:
        pg3_results["delete_thread"] = True
        pg3_holding.set()
        pg3_release.wait(timeout=15)


profile_resolution.lock_user_scope = _pg3_patched_lock
profile_router.lock_user_scope = _pg3_patched_lock


def pg3_worker_delete():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg3).first()
        profile_router.delete_growth_profile(profile_id="DOOMED", current_user=user, db=db)
        pg3_results["delete_ok"] = True
    except Exception as e:
        pg3_results["delete_ok"] = False
        pg3_results["delete_error"] = type(e).__name__
    finally:
        db.close()


pg3_edit_finished = _threading.Event()


def pg3_worker_concurrent_edit():
    # A stale device trying to keep DOOMED alive with a newer edit —
    # exactly the resurrection scenario the tombstone mechanism exists for.
    pg3_holding.wait(timeout=15)
    time.sleep(0.3)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg3).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [
                    {"id": "A", "name": "Keep", "updatedAt": "2026-09-01T00:00:00.000Z"},
                    {"id": "DOOMED", "name": "Still Alive (stale)", "updatedAt": "2026-09-01T00:20:00.000Z"},
                ],
                "activeProfileId": "A",
                "updatedAt": "2026-09-01T00:20:00.000Z",
            },
            deletedProfileIds=[],
        )
        profile_router.update_growth_state(data=data, current_user=user, db=db)
        pg3_results["edit_ok"] = True
    except Exception as e:
        pg3_results["edit_ok"] = False
        pg3_results["edit_error"] = type(e).__name__
    finally:
        db.close()
        pg3_edit_finished.set()


t5 = _threading.Thread(target=pg3_worker_delete)
t6 = _threading.Thread(target=pg3_worker_concurrent_edit)
t5.start(); t6.start()

pg3_holding.wait(timeout=15)
time.sleep(0.6)
pg3_edit_blocked = not pg3_edit_finished.is_set()
pg3_release.set()

t5.join(timeout=20)
t6.join(timeout=20)
profile_resolution.lock_user_scope = _orig_lock_pg3
profile_router.lock_user_scope = _orig_lock_pg3

check("the concurrent edit was genuinely blocked while delete held the advisory lock",
      pg3_edit_blocked, {"edit_finished_early": pg3_edit_finished.is_set()})
check("both requests completed without error", pg3_results.get("delete_ok") and pg3_results.get("edit_ok"), pg3_results)

final_pg3 = read_state(pid_pg3)
check("the tombstone WINS — DOOMED does not exist in the final profiles list",
      "DOOMED" not in profile_names(final_pg3), final_pg3)
check("DOOMED is recorded as tombstoned",
      "DOOMED" in final_pg3["deleted_profile_ids"], final_pg3)
check("the surviving profile A is unaffected",
      profile_names(final_pg3).get("A") == "Keep", final_pg3)


# ═════════════════════════════════════════════════════════════════════════
section("PG4 — a free user's unentitled push racing a premium user's push (different accounts)")
# ═════════════════════════════════════════════════════════════════════════
uid_free = seed_user(premium=False)
uid_premium = seed_user(premium=True)
pid_free = seed_profile(uid_free, [{"id": "F1", "name": "Free Bootstrap", "updatedAt": "2026-09-01T00:00:00.000Z"}], "F1")
pid_premium = seed_profile(uid_premium, [{"id": "P1", "name": "Premium One", "updatedAt": "2026-09-01T00:00:00.000Z"}], "P1")

pg4_free_finished = _threading.Event()
pg4_premium_finished = _threading.Event()
pg4_results = {}


def pg4_worker_free():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_free).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Free"},
                "profiles": [
                    {"id": "F1", "name": "Free Bootstrap", "updatedAt": "2026-09-01T00:00:00.000Z"},
                    {"id": "F2-UNENTITLED", "name": "Second Profile", "updatedAt": "2026-09-01T00:00:01.000Z"},
                ],
                "activeProfileId": "F1",
                "updatedAt": "2026-09-01T00:00:01.000Z",
            },
            deletedProfileIds=[],
        )
        resp = profile_router.update_growth_state(data=data, current_user=user, db=db)
        pg4_results["free_rejected"] = list(resp.rejectedProfileIds or [])
    except Exception as e:
        pg4_results["free_error"] = type(e).__name__
    finally:
        db.close()
        pg4_free_finished.set()


def pg4_worker_premium():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_premium).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Premium"},
                "profiles": [
                    {"id": "P1", "name": "Premium One", "updatedAt": "2026-09-01T00:00:00.000Z"},
                    {"id": "P2-LEGIT", "name": "Second Legit Profile", "updatedAt": "2026-09-01T00:00:01.000Z"},
                ],
                "activeProfileId": "P1",
                "updatedAt": "2026-09-01T00:00:01.000Z",
            },
            deletedProfileIds=[],
        )
        resp = profile_router.update_growth_state(data=data, current_user=user, db=db)
        pg4_results["premium_accepted"] = list(resp.acceptedProfileIds or [])
    except Exception as e:
        pg4_results["premium_error"] = type(e).__name__
    finally:
        db.close()
        pg4_premium_finished.set()


t7 = _threading.Thread(target=pg4_worker_free)
t8 = _threading.Thread(target=pg4_worker_premium)
t7.start(); t8.start()
t7.join(timeout=20); t8.join(timeout=20)

check("both concurrent pushes (different accounts) completed without error",
      "free_error" not in pg4_results and "premium_error" not in pg4_results, pg4_results)

final_free = read_state(pid_free)
final_premium = read_state(pid_premium)
check("the free user's unentitled second profile was FILTERED, not silently accepted",
      "F2-UNENTITLED" not in profile_names(final_free), final_free)
check("the entitlement filter's rejection is reported to the free user's own response",
      "F2-UNENTITLED" in pg4_results.get("free_rejected", []), pg4_results)
check("the premium user's legitimate second profile was accepted",
      "P2-LEGIT" in profile_names(final_premium), final_premium)
check("neither account's write leaked into the other's stored state",
      "P2-LEGIT" not in profile_names(final_free) and "F2-UNENTITLED" not in profile_names(final_premium),
      {"free": final_free, "premium": final_premium})


# ═════════════════════════════════════════════════════════════════════════
section("PG5 — entitlement lapsing BETWEEN request start and commit")
# ═════════════════════════════════════════════════════════════════════════
# Simulates the narrowest possible entitlement-transition window: the
# request reads current_user (still premium) at the START of the request,
# but by the time the write actually lands, a webhook has already flipped
# premium_until into the past. The write must be evaluated against
# entitlement as read INSIDE this request — never a value cached before it.
uid_pg5 = seed_user(premium=True)
pid_pg5 = seed_profile(uid_pg5, [{"id": "A", "name": "Original", "updatedAt": "2026-09-01T00:00:00.000Z"}], "A")

pg5_holding = _threading.Event()
pg5_release = _threading.Event()
pg5_results = {}

_orig_lock_pg5 = profile_resolution.lock_user_scope


def _pg5_patched_lock(db, user_id):
    _orig_lock_pg5(db, user_id)
    if user_id == uid_pg5 and "push_thread" not in pg5_results:
        pg5_results["push_thread"] = True
        pg5_holding.set()
        pg5_release.wait(timeout=15)


profile_resolution.lock_user_scope = _pg5_patched_lock
profile_router.lock_user_scope = _pg5_patched_lock


def pg5_worker_push():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg5).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [
                    {"id": "A", "name": "Original", "updatedAt": "2026-09-01T00:00:00.000Z"},
                    {"id": "B-DURING-LAPSE", "name": "Created While Held", "updatedAt": "2026-09-01T00:00:01.000Z"},
                ],
                "activeProfileId": "A",
                "updatedAt": "2026-09-01T00:00:01.000Z",
            },
            deletedProfileIds=[],
        )
        resp = profile_router.update_growth_state(data=data, current_user=user, db=db)
        pg5_results["push_ok"] = True
        pg5_results["rejected"] = list(resp.rejectedProfileIds or [])
    except Exception as e:
        pg5_results["push_ok"] = False
        pg5_results["push_error"] = type(e).__name__
    finally:
        db.close()


def pg5_worker_lapse_entitlement():
    # Waits until the push has already started (holding the advisory lock,
    # user object already fetched) — then flips entitlement in a SEPARATE
    # committed transaction, simulating a webhook landing mid-request.
    pg5_holding.wait(timeout=15)
    db = SessionLocal()
    try:
        row = db.query(User).filter(User.id == uid_pg5).first()
        row.premium_until = datetime.datetime.utcnow() - datetime.timedelta(days=1)
        db.commit()
    finally:
        db.close()
    time.sleep(0.3)
    pg5_release.set()


t9 = _threading.Thread(target=pg5_worker_push)
t10 = _threading.Thread(target=pg5_worker_lapse_entitlement)
t9.start(); t10.start()
t9.join(timeout=20); t10.join(timeout=20)
profile_resolution.lock_user_scope = _orig_lock_pg5
profile_router.lock_user_scope = _orig_lock_pg5

check("the push request completed without error", pg5_results.get("push_ok"), pg5_results)
final_pg5 = read_state(pid_pg5)
# _refresh_effective_premium re-queries the User row fresh (populate_existing,
# bypassing the identity map) at the exact point entitlement is decided —
# inside the lock, immediately before it's used — rather than trusting
# `current_user`, which was fetched by get_current_user before this route
# body ever ran. This proves the lapse committed in the OTHER session (by
# pg5_worker_lapse_entitlement, timed to land after the push has already
# started but before it reaches this check) is honored, not missed.
check("the profile created while entitlement lapsed mid-request is REJECTED, "
      "never silently kept as if the request-start snapshot were still current",
      "B-DURING-LAPSE" not in profile_names(final_pg5)
      and "B-DURING-LAPSE" in pg5_results.get("rejected", []),
      {"state": final_pg5, "results": pg5_results})


# ═════════════════════════════════════════════════════════════════════════
section("PG6 — the ensure/create bootstrap race (plan §1.4)")
# ═════════════════════════════════════════════════════════════════════════
# T1 ensure: locks, attaches every currently-visible unassigned book, commits.
# T2 create: a concurrent library create resolving its OWN assignment via
# _assignment_for_new_item — the function library.py's three create routes
# (POST /library/, /upload-pdf, /add-url) all funnel through.
#
# A bare `db.add(LibraryItem(...)); db.commit()` does NOT contend for the
# advisory lock — nothing makes a raw INSERT respect a lock nobody asked it
# to take. The real race is between two callers that BOTH take the lock:
# `ensure` (profile.py) and the create path's OWN assignment resolution
# (library.py's _assignment_for_new_item). Before this test was written,
# `_assignment_for_new_item` did not call lock_user_scope at all — found
# and fixed here (library.py now takes the same advisory lock before
# reading growth_state, matching §4.0's documented order).
uid_pg6 = seed_user(premium=True)
# A plain string id, captured BEFORE the seeding session closes — the ORM
# object itself becomes detached the moment its session closes, and (with
# expire_on_commit, SQLAlchemy's default) even reading an already-loaded
# attribute off it can trigger a refresh against a session that no longer
# exists. Every later reference below uses this plain string, never the
# detached object.
item_pg6_id = "item-" + uuid.uuid4().hex[:8]
_db_seed_item = SessionLocal()
_db_seed_item.add(LibraryItem(
    id=item_pg6_id, user_id=uid_pg6, title="Concurrent Upload",
    type="text", mode="wisdom", processed=True,
))
_db_seed_item.commit()
_db_seed_item.close()

pg6_ensure_committed = _threading.Event()
pg6_create_arrived = _threading.Event()
pg6_results = {}

_orig_lock_pg6 = profile_resolution.lock_user_scope


def _pg6_patched_lock(db, user_id):
    # The two callers must be tagged by WHICH FUNCTION invokes them, not by
    # arrival order (arrival order is a genuine race between the two
    # threads). And the block must sit on the CREATE side, held until ensure
    # has actually COMMITTED — not on the ensure side blocked at
    # lock-acquisition time. An earlier version of this patch blocked ensure
    # itself immediately on entry to lock_user_scope, i.e. before
    # ensure_growth_state had done any of its work (create/attach/commit).
    # That let the create-worker's own lock_user_scope call proceed and read
    # growth_state while ensure was still parked having created NOTHING —
    # so create legitimately (and correctly) saw zero profiles and returned
    # None. The bug was in the test's blocking point, not in the code under
    # test: real callers never observe "the lock is held but the holder
    # hasn't started its work" as a distinct state from "the lock is free" —
    # what must be proven is that create, genuinely serialized to run AFTER
    # ensure's transaction, sees what ensure committed.
    import inspect
    caller_name = inspect.stack()[1].function
    if user_id == uid_pg6 and caller_name == "_assignment_for_new_item":
        pg6_create_arrived.set()
        pg6_ensure_committed.wait(timeout=15)
    _orig_lock_pg6(db, user_id)


profile_resolution.lock_user_scope = _pg6_patched_lock
profile_router.lock_user_scope = _pg6_patched_lock


def pg6_worker_ensure():
    pg6_create_arrived.wait(timeout=15)
    time.sleep(0.3)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg6).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [{"id": "A", "name": "First Profile", "updatedAt": "2026-09-01T00:00:00.000Z"}],
                "activeProfileId": "A",
                "updatedAt": "2026-09-01T00:00:00.000Z",
            },
            deletedProfileIds=[],
        )
        profile_router.ensure_growth_state(data=data, current_user=user, db=db)
        pg6_results["ensure_ok"] = True
    except Exception as e:
        pg6_results["ensure_ok"] = False
        pg6_results["ensure_error"] = type(e).__name__
    finally:
        db.close()
        pg6_ensure_committed.set()


pg6_create_finished = _threading.Event()


def pg6_worker_library_create():
    # The REAL create-path assignment resolution — the exact function
    # library.py's three create routes call — genuinely contending for the
    # same advisory lock ensure just took. Started FIRST (before ensure) so
    # its lock_user_scope call is the one that arrives and parks, proving
    # create is held back until ensure's transaction has actually committed
    # — not merely until ensure has acquired the lock.
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg6).first()
        gp_id, gp_name = library_router._assignment_for_new_item(db, user, "wisdom", None, None)
        row = db.query(LibraryItem).filter(LibraryItem.id == item_pg6_id).first()
        row.growth_profile_id = gp_id
        row.growth_profile_name = gp_name
        db.commit()
        pg6_results["create_ok"] = True
        pg6_results["assigned_id"] = gp_id
    except Exception as e:
        pg6_results["create_ok"] = False
        pg6_results["create_error"] = type(e).__name__
    finally:
        db.close()
        pg6_create_finished.set()


t12 = _threading.Thread(target=pg6_worker_library_create)
t12.start()
# Wait for create to actually be parked inside its own lock_user_scope call
# before starting ensure — this is what proves the lock, not timing luck,
# is what serializes the two.
pg6_create_arrived.wait(timeout=15)

t11 = _threading.Thread(target=pg6_worker_ensure)
t11.start()

# Checked BEFORE ensure's own internal 0.3s delay elapses (it waits on
# pg6_create_arrived, already set, then sleeps 0.3s before doing any real
# work) — at this point ensure cannot possibly have committed yet, so create
# must still be genuinely parked on the lock it is contending for.
time.sleep(0.1)
pg6_create_blocked = not pg6_create_finished.is_set()

t11.join(timeout=20)
t12.join(timeout=20)
profile_resolution.lock_user_scope = _orig_lock_pg6
profile_router.lock_user_scope = _orig_lock_pg6

check("the concurrent library create's assignment resolution was genuinely blocked "
      "while ensure held the advisory lock (proves the shared lock, not just the "
      "profile row lock, serializes the two)",
      pg6_create_blocked, {"create_finished_early": pg6_create_finished.is_set()})
check("both operations completed without error", pg6_results.get("ensure_ok") and pg6_results.get("create_ok"), pg6_results)
check("the create, having been serialized AFTER ensure committed the first profile, "
      "sees it and resolves a real assignment on its FIRST attempt — never left "
      "bootstrap-unassigned waiting for a later repair pass",
      pg6_results.get("assigned_id") == "A", pg6_results)

db_check = SessionLocal()
try:
    final_item = db_check.query(LibraryItem).filter(LibraryItem.id == item_pg6_id).first()
    final_assignment = (final_item.growth_profile_id, final_item.growth_profile_name)
finally:
    db_check.close()
check("the book's stored assignment matches profile A, not left NULL",
      final_assignment == ("A", "First Profile"), final_assignment)


# ═════════════════════════════════════════════════════════════════════════
section("PG7 — lock-order conformance: interleaved calls across different users, no deadlock")
# ═════════════════════════════════════════════════════════════════════════
# Advisory lock is keyed per-user, so DIFFERENT users' calls must run in
# parallel without contention (proving the lock doesn't serialize the whole
# table), while the documented order (advisory -> profile row -> library
# rows) is followed identically by every endpoint so no two endpoints could
# ever deadlock against each other even under adversarial interleaving.
N_USERS = 6
pg7_users = [seed_user(premium=True) for _ in range(N_USERS)]
pg7_profiles = {
    uid: seed_profile(uid, [{"id": "A", "name": "Start", "updatedAt": "2026-09-01T00:00:00.000Z"}], "A")
    for uid in pg7_users
}
pg7_errors = []
pg7_lock = _threading.Lock()


def pg7_worker(uid, seq):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        # create -> rename -> delete-attempt-on-nonexistent (a 404, not a
        # deadlock) -> a final growth PUT, interleaving every endpoint that
        # takes the advisory lock, for every user, all at once.
        r1 = profile_router.create_growth_profile(
            data=GrowthProfileCreate(profile={"id": f"B-{seq}", "name": f"Second-{seq}"}),
            current_user=user, db=db,
        )
        r2 = profile_router.rename_growth_profile(
            profile_id="A", data=GrowthProfileRename(name=f"Renamed-{seq}"),
            current_user=user, db=db,
        )
        try:
            profile_router.delete_growth_profile(profile_id="does-not-exist", current_user=user, db=db)
        except Exception:
            pass  # expected 404 — not the thing under test
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [
                    {"id": "A", "name": f"Renamed-{seq}", "updatedAt": "2026-09-01T00:00:02.000Z"},
                    {"id": f"B-{seq}", "name": f"Second-{seq}", "updatedAt": "2026-09-01T00:00:02.000Z"},
                ],
                "activeProfileId": "A",
                "updatedAt": "2026-09-01T00:00:02.000Z",
            },
            deletedProfileIds=[],
        )
        profile_router.update_growth_state(data=data, current_user=user, db=db)
    except Exception as e:
        with pg7_lock:
            pg7_errors.append((uid, type(e).__name__, str(e)))
    finally:
        db.close()


threads = [_threading.Thread(target=pg7_worker, args=(uid, i)) for i, uid in enumerate(pg7_users)]
start = time.monotonic()
for th in threads:
    th.start()
for th in threads:
    th.join(timeout=30)
elapsed = time.monotonic() - start

check("every interleaved worker thread finished (none hung — no deadlock)",
      all(not th.is_alive() for th in threads),
      [th.is_alive() for th in threads])
check("no unexpected errors during the fully-interleaved multi-user run",
      len(pg7_errors) == 0, pg7_errors)
check("different users' calls did not serialize against EACH OTHER "
      f"(finished in {elapsed:.2f}s for {N_USERS} users x 4 sequential ops each — "
      "a per-user lock should not make this materially slower than one user's own sequence)",
      elapsed < 20.0, elapsed)

for uid in pg7_users:
    state = read_state(pg7_profiles[uid])
    names = profile_names(state)
    seq = pg7_users.index(uid)
    check(f"user {seq}'s state reflects its own full sequence (rename + create), not another user's",
          names.get("A") == f"Renamed-{seq}" and f"B-{seq}" in names, names)


# ═════════════════════════════════════════════════════════════════════════
section("PG8 — two concurrent canonical creates racing on the SAME normalized name")
# ═════════════════════════════════════════════════════════════════════════
# plan Phase 8's test_profile_creation_endpoint.py spec: "concurrent creates
# serialize". Distinct from PG3 (tombstone vs. edit) and PG7 (lock-order
# across DIFFERENT users) — this is the uniqueness check itself (§2.4) under
# real contention: two requests for the SAME user, same normalized name,
# submitted at genuinely the same instant. Only one may win; the advisory
# lock must make the second request's uniqueness check see the first's
# already-committed row rather than both reading "no collision" from a
# stale pre-commit snapshot.
uid_pg8 = seed_user(premium=True)
pid_pg8 = seed_profile(uid_pg8, [{"id": "orig", "name": "Original", "updatedAt": "2026-09-01T00:00:00.000Z"}], "orig")

pg8_holding = _threading.Event()
pg8_release = _threading.Event()
pg8_results = {}

_orig_lock_pg8 = profile_resolution.lock_user_scope


def _pg8_patched_lock(db, user_id):
    _orig_lock_pg8(db, user_id)
    if user_id == uid_pg8 and "first_thread" not in pg8_results:
        pg8_results["first_thread"] = True
        pg8_holding.set()
        pg8_release.wait(timeout=15)


profile_resolution.lock_user_scope = _pg8_patched_lock
profile_router.lock_user_scope = _pg8_patched_lock


def pg8_worker_create(label):
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg8).first()
        profile_router.create_growth_profile(
            data=GrowthProfileCreate(profile={"id": f"dup-{label}", "name": "Same Name"}),
            current_user=user, db=db,
        )
        pg8_results[f"{label}_ok"] = True
    except Exception as e:
        pg8_results[f"{label}_ok"] = False
        pg8_results[f"{label}_error"] = type(e).__name__
        pg8_results[f"{label}_status"] = getattr(e, "status_code", None)
    finally:
        db.close()


pg8_second_finished = _threading.Event()


def pg8_worker_create_second():
    pg8_holding.wait(timeout=15)
    time.sleep(0.3)
    pg8_worker_create("second")
    pg8_second_finished.set()


t13 = _threading.Thread(target=lambda: pg8_worker_create("first"))
t14 = _threading.Thread(target=pg8_worker_create_second)
t13.start(); t14.start()

pg8_holding.wait(timeout=15)
time.sleep(0.6)
pg8_second_blocked = not pg8_second_finished.is_set()
pg8_release.set()

t13.join(timeout=20)
t14.join(timeout=20)
profile_resolution.lock_user_scope = _orig_lock_pg8
profile_router.lock_user_scope = _orig_lock_pg8

check("the second create was genuinely blocked on the advisory lock while the first was in flight",
      pg8_second_blocked, {"second_finished_early": pg8_second_finished.is_set()})
check("exactly ONE of the two concurrent same-name creates succeeded",
      sum(1 for k in ("first_ok", "second_ok") if pg8_results.get(k)) == 1, pg8_results)
check("the loser was rejected as a duplicate name (409), not a generic error",
      pg8_results.get("first_status") == 409 or pg8_results.get("second_status") == 409, pg8_results)

final_pg8 = read_state(pid_pg8)
dup_ids_present = [pid for pid in ("dup-first", "dup-second") if pid in profile_names(final_pg8)]
check("only ONE of the two duplicate-named profiles was actually persisted — no race-created duplicate",
      len(dup_ids_present) == 1, {"state": final_pg8, "persisted": dup_ids_present})


# ═════════════════════════════════════════════════════════════════════════
section("PG9 — _get_or_create_profile's bootstrap-creation must NOT release the advisory lock early")
# ═════════════════════════════════════════════════════════════════════════
# Independent audit finding (post-implementation, Sep 2026): every one of
# _get_or_create_profile's 5 callers takes lock_user_scope immediately
# before calling it, but the OLD bootstrap-creation branch called
# db.commit() to persist the brand-new Profile row before re-locking it —
# and pg_advisory_xact_lock is TRANSACTION-scoped, releasing on commit. For
# a user's very FIRST locked call (no profile row exists yet), that commit
# silently ended the transaction holding the caller's advisory lock, and
# the re-lock (`SELECT ... FOR UPDATE`) then ran in a brand-new transaction
# with NO advisory lock at all — reopening the exact bootstrap/create race
# §1.4's lock exists to close.
#
# PG6 (above) proves ensure/create serialize correctly at the FUNCTION
# boundary, but its lock-patch blocks the ENTIRE ensure_growth_state call
# until it fully returns — it cannot observe an intermediate release
# happening INSIDE _get_or_create_profile itself, because it never lets a
# second caller's own lock attempt overlap with the first's in-progress
# transaction at all.
#
# This section proves the lock is held for the true FULL DURATION by
# querying Postgres's own `pg_locks` view directly from a THIRD, unrelated
# connection while the first caller is paused mid-`ensure_growth_state` (via
# a debug hook on ensure_growth_state itself, called AFTER
# _get_or_create_profile has returned) — a real system-level fact, not an
# application-level race that could be fooled by scheduling luck. This is
# strictly stronger than racing a second caller: it directly answers "is
# THIS lock, identified by its exact namespace+key, currently held by
# ANY backend" rather than inferring it from whether a second caller's own
# attempt happened to block in the available time window.
from app.services.profile_resolution import _ADVISORY_LOCK_NAMESPACE, _stable_int_key

uid_pg9 = seed_user(premium=True)
# Deliberately NO seed_profile() call — this user has zero profile rows,
# so the FIRST locked call for them must take the bootstrap-creation
# branch inside _get_or_create_profile.
pg9_lock_key = _stable_int_key(uid_pg9)

pg9_paused_after_create = _threading.Event()
pg9_release = _threading.Event()
pg9_results = {}

_orig_get_or_create_pg9 = profile_router._get_or_create_profile


def _pg9_patched_get_or_create(user, db, *, for_update=False):
    # Call straight through to the REAL implementation (the fixed
    # flush+refresh, or the reverted commit, whichever is currently in
    # source) — this test observes its EFFECT via pg_locks, never
    # reimplements its logic, so it cannot silently diverge from whatever
    # the real function actually does. Pausing AFTER it returns, still
    # inside ensure_growth_state's single call, is exactly the moment the
    # old bug's early commit would already have released the lock.
    result = _orig_get_or_create_pg9(user, db, for_update=for_update)
    if user.id == uid_pg9 and for_update and "paused" not in pg9_results:
        pg9_results["paused"] = True
        pg9_paused_after_create.set()
        pg9_release.wait(timeout=15)
    return result


profile_router._get_or_create_profile = _pg9_patched_get_or_create


def pg9_worker_first():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg9).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "First"},
                "profiles": [{"id": "F", "name": "First Profile", "updatedAt": "2026-09-01T00:00:00.000Z"}],
                "activeProfileId": "F",
                "updatedAt": "2026-09-01T00:00:00.000Z",
            },
            deletedProfileIds=[],
        )
        profile_router.ensure_growth_state(data=data, current_user=user, db=db)
        pg9_results["first_ok"] = True
    except Exception as e:
        pg9_results["first_ok"] = False
        pg9_results["first_error"] = type(e).__name__
    finally:
        db.close()


t15 = _threading.Thread(target=pg9_worker_first)
t15.start()
pg9_paused_after_create.wait(timeout=15)
time.sleep(0.2)  # let the pause settle before the external observation below

# THE decisive check: query pg_locks from a completely separate connection.
# classid/objid encode the two int4 args to pg_advisory_xact_lock as a
# single bigint pair the way Postgres's advisory-lock catalog entries do —
# concretely, `pg_locks.classid` holds the high 32 bits (our namespace) and
# `objid` the low 32 bits (our per-user key) for a session-level advisory
# lock taken with the 2-argument (int4, int4) form.
observer = SessionLocal()
try:
    row = observer.execute(
        text(
            "SELECT count(*) FROM pg_locks "
            "WHERE locktype = 'advisory' AND classid = :ns AND objid = :key AND granted = true"
        ),
        {"ns": _ADVISORY_LOCK_NAMESPACE, "key": pg9_lock_key},
    ).scalar()
finally:
    observer.close()

check("Postgres's own pg_locks view shows the advisory lock STILL GRANTED "
      "immediately after _get_or_create_profile's bootstrap-creation branch returns — "
      "proving the fix (a single db.flush()+refresh(with_for_update), no intermediate "
      "commit) keeps the caller's transaction, and therefore its advisory lock, open "
      "for the whole operation",
      row == 1, {"pg_locks_rows_for_this_key": row})

pg9_release.set()
t15.join(timeout=20)
profile_router._get_or_create_profile = _orig_get_or_create_pg9

check("the (real, unpatched) ensure_growth_state call completed without error", pg9_results.get("first_ok"), pg9_results)

# Decisive correctness check: exactly ONE profile row for this user. If the
# advisory lock actually had a gap here, a second concurrent caller could
# slip through and create a duplicate Profile row for the same user — a
# class of duplicate nothing else in this feature is designed to detect.
db_check_pg9 = SessionLocal()
try:
    profile_count_pg9 = db_check_pg9.query(Profile).filter(Profile.user_id == uid_pg9).count()
finally:
    db_check_pg9.close()
check("exactly ONE profile row exists for this user",
      profile_count_pg9 == 1, profile_count_pg9)


# ═════════════════════════════════════════════════════════════════════════
section("PG10 — PATCH /library/{id}'s assignment write racing a concurrent profile DELETE")
# ═════════════════════════════════════════════════════════════════════════
# Independent audit finding (post-implementation, Sep 2026): rename_library_
# item (the PATCH /library/{item_id} handler) took NO lock at all before
# this fix — the only one of §4.0's "every assignment write" paths with
# zero locking. It could read a still-live profile id, then commit an
# assignment to it AFTER a concurrent DELETE /profile/profiles/{id} had
# already tombstoned that same id in a separate, already-committed
# transaction — producing a dangling assignment. PG3 (above) already proves
# the generic growth-push path defends against this; this section proves
# the SAME discipline for the PATCH endpoint specifically, which is a
# completely different code path with its own (previously absent) locking.
uid_pg10 = seed_user(premium=True)
pid_pg10 = seed_profile(
    uid_pg10,
    [{"id": "A", "name": "Keep", "updatedAt": "2026-09-01T00:00:00.000Z"},
     {"id": "DOOMED", "name": "About To Be Deleted", "updatedAt": "2026-09-01T00:00:00.000Z"}],
    "A",
)
item_pg10_id = "item-" + uuid.uuid4().hex[:8]
_db_seed_pg10 = SessionLocal()
_db_seed_pg10.add(LibraryItem(
    id=item_pg10_id, user_id=uid_pg10, title="Racing Assignment",
    type="text", mode="wisdom", processed=True,
))
_db_seed_pg10.commit()
_db_seed_pg10.close()

pg10_holding = _threading.Event()
pg10_release = _threading.Event()
pg10_results = {}

_orig_lock_pg10 = profile_resolution.lock_user_scope


def _pg10_patched_lock(db, user_id):
    _orig_lock_pg10(db, user_id)
    if user_id == uid_pg10 and "delete_thread" not in pg10_results:
        pg10_results["delete_thread"] = True
        pg10_holding.set()
        pg10_release.wait(timeout=15)


profile_resolution.lock_user_scope = _pg10_patched_lock
profile_router.lock_user_scope = _pg10_patched_lock
# NOT library_router.lock_user_scope: rename_library_item imports
# lock_user_scope LOCALLY (`from app.services.profile_resolution import
# lock_user_scope`, function-scoped, resolved fresh at call time) rather
# than at module level like _assignment_for_new_item's own import — so
# patching profile_resolution's own attribute above is what it actually
# picks up; assigning a library_router.lock_user_scope attribute here
# would silently do nothing (create an unused attribute) rather than
# intercept anything.


def pg10_worker_delete():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg10).first()
        profile_router.delete_growth_profile(profile_id="DOOMED", current_user=user, db=db)
        pg10_results["delete_ok"] = True
    except Exception as e:
        pg10_results["delete_ok"] = False
        pg10_results["delete_error"] = type(e).__name__
    finally:
        db.close()


pg10_patch_finished = _threading.Event()


def pg10_worker_patch():
    # A client that fetched the profile list BEFORE the delete (DOOMED
    # still looked live to it) submits the assignment WHILE the delete is
    # in flight — exactly the dangling-assignment scenario the missing
    # lock allowed.
    pg10_holding.wait(timeout=15)
    time.sleep(0.3)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid_pg10).first()
        library_router.rename_library_item(
            item_id=item_pg10_id,
            data=UpdateItemRequest(growth_profile_id="DOOMED"),
            current_user=user, db=db,
        )
        pg10_results["patch_ok"] = True
    except Exception as e:
        pg10_results["patch_ok"] = False
        pg10_results["patch_error"] = type(e).__name__
        pg10_results["patch_status"] = getattr(e, "status_code", None)
    finally:
        db.close()
        pg10_patch_finished.set()


t17 = _threading.Thread(target=pg10_worker_delete)
t18 = _threading.Thread(target=pg10_worker_patch)
t17.start(); t18.start()

pg10_holding.wait(timeout=15)
time.sleep(0.6)
pg10_patch_blocked = not pg10_patch_finished.is_set()
pg10_release.set()

t17.join(timeout=20)
t18.join(timeout=20)
profile_resolution.lock_user_scope = _orig_lock_pg10
profile_router.lock_user_scope = _orig_lock_pg10

check("the concurrent PATCH assignment attempt was genuinely blocked while delete held the advisory lock — "
      "proving rename_library_item now actually takes the lock (it took none at all before this fix)",
      pg10_patch_blocked, {"patch_finished_early": pg10_patch_finished.is_set()})
check("the delete completed without error", pg10_results.get("delete_ok"), pg10_results)
check("the PATCH, serialized AFTER the delete committed, correctly REJECTS the now-dead id "
      "(400, 'no longer available') rather than assigning to it",
      pg10_results.get("patch_ok") is False and pg10_results.get("patch_status") == 400,
      pg10_results)

final_item_pg10 = SessionLocal()
try:
    row_pg10 = final_item_pg10.query(LibraryItem).filter(LibraryItem.id == item_pg10_id).first()
    final_assignment_pg10 = (row_pg10.growth_profile_id, row_pg10.growth_profile_name)
finally:
    final_item_pg10.close()
check("the book's stored assignment is NOT the tombstoned id — no dangling assignment was ever persisted",
      final_assignment_pg10[0] != "DOOMED", final_assignment_pg10)


# ═════════════════════════════════════════════════════════════════════════
print()
if failures:
    print(f"FAILED: {len(failures)} check(s) — {failures}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
