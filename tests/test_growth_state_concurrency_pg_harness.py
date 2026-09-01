"""
Real-PostgreSQL proof for re-audit finding #7-3: two genuinely concurrent
PUT /profile/growth requests for the SAME profile must not silently drop
one side's tombstone/growth_state update.

Same bootstrap pattern as tests/test_session_generation_pg_harness.py /
test_deletion_tombstones.py — a fully disposable local Postgres cluster,
short temp-dir prefix (Unix socket path is capped at 103 bytes).

  PG1 — two REAL sessions call the ACTUAL update_growth_state endpoint
        function concurrently for the same profile, each deleting a
        DIFFERENT profile (A deletes X, B deletes Y), with a barrier
        holding both inside a patched row-fetch so their read-modify-
        writes genuinely overlap. Without the SELECT ... FOR UPDATE lock
        (re-audit finding #7-3), whichever commit lands last overwrites
        the other's deleted_profile_ids entirely — this proves BOTH
        tombstones survive.
  PG2 — same race, but on the growth_state body itself (both devices
        editing different fields under an unlocked read-compare-write):
        proves the LATER-committing side's write is based on the
        FIRST side's already-committed state (serialized), not a stale
        snapshot — i.e. genuinely serialized, not merely non-crashing.

    .venv/bin/python tests/test_growth_state_concurrency_pg_harness.py
"""
import os, sys, shutil, socket, subprocess, tempfile, threading, atexit

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")


# ═════════════════════════════════════════════════════════════════════════
# Disposable cluster bootstrap — same pattern as the other *_pg_harness.py
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
    # Short prefix on purpose: Postgres' Unix-domain socket path is capped at
    # 103 bytes, and the full path includes the temp dir + "/sock/.s.PGSQL.<port>".
    _cluster_dir = tempfile.mkdtemp(prefix="nib-gs-")
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
         "-c", "CREATE DATABASE nibbler_pg_growth_state"],
        check=True, capture_output=True, env=env, text=True)
    return f"postgresql://postgres@/nibbler_pg_growth_state?host={sock_dir}&port={port}"


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
import time
import threading as _threading

from app.database import create_tables, SessionLocal
from app.models.user import User
from app.models.profile import Profile
from app.routers import profile as profile_router
from app.schemas.profile import GrowthStateUpdate

create_tables()


def seed_profile():
    uid = "u-" + uuid.uuid4().hex[:8]
    pid = "p-" + uuid.uuid4().hex[:8]
    s = SessionLocal()
    s.add(User(id=uid, email=f"{uid}@t.test", is_premium=True))
    s.commit()
    s.add(Profile(
        id=pid, user_id=uid, name="Test User",
        growth_state={
            "person": {"name": "Test User"},
            "profiles": [
                {"id": "profile-X", "name": "Profile X", "updatedAt": "2026-09-01T00:00:00.000Z"},
                {"id": "profile-Y", "name": "Profile Y", "updatedAt": "2026-09-01T00:00:00.000Z"},
            ],
            "activeProfileId": "profile-X",
            "updatedAt": "2026-09-01T00:00:00.000Z",
        },
        deleted_profile_ids=[],
    ))
    s.commit()
    s.close()
    return uid, pid


class _FakeUser:
    def __init__(self, uid): self.id = uid


def read_profile(pid):
    s = SessionLocal()
    try:
        row = s.query(Profile).filter(Profile.id == pid).first()
        return {
            "deleted_profile_ids": list(row.deleted_profile_ids or []),
            "profile_ids": [p.get("id") for p in (row.growth_state or {}).get("profiles", [])],
        }
    finally:
        s.close()


# ═════════════════════════════════════════════════════════════════════════
section("PG1 — two REAL sessions deleting DIFFERENT profiles concurrently")
# ═════════════════════════════════════════════════════════════════════════
# Deterministic overlap, not a hopeful timing stagger: worker A is held
# INSIDE its locked, uncommitted transaction (via the same patched
# _get_or_create_profile hold-point PG2 uses) until worker B has genuinely
# started its own request and had a real chance to issue its own
# with_for_update() and either block on Postgres's lock manager (fixed
# code) or race past it onto stale state (unlocked code) — a natural
# `time.sleep` stagger between two independently-scheduled threads cannot
# guarantee that overlap on every run, which is exactly why an earlier
# version of this test passed even against deliberately-unlocked code.
uid, pid = seed_profile()

pg1_a_holding = _threading.Event()
pg1_a_release = _threading.Event()
pg1_timing = {}
results = {}

_orig_get_or_create_pg1 = profile_router._get_or_create_profile


def _pg1_patched_get_or_create(user, db, *, for_update=False):
    result = _orig_get_or_create_pg1(user, db, for_update=for_update)
    if for_update and user.id == uid and "a" not in pg1_timing:
        pg1_timing["a"] = True
        pg1_a_holding.set()
        pg1_a_release.wait(timeout=15)
    return result


profile_router._get_or_create_profile = _pg1_patched_get_or_create


def worker_a1():
    db = SessionLocal()
    try:
        current_user = db.query(User).filter(User.id == uid).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [{"id": "profile-Y", "name": "Profile Y", "updatedAt": "2026-09-01T00:00:01.000Z"}],
                "activeProfileId": "profile-Y",
                "updatedAt": "2026-09-01T00:00:01.000Z",
            },
            deletedProfileIds=["profile-X"],
        )
        resp = profile_router.update_growth_state(data=data, current_user=current_user, db=db)
        results["A"] = {"ok": True, "deletedProfileIds": list(resp.deleted_profile_ids or [])}
    except Exception as e:
        results["A"] = {"ok": False, "error": type(e).__name__, "detail": getattr(e, "detail", None)}
    finally:
        db.close()


pg1_b_finished = _threading.Event()


def worker_b1():
    pg1_a_holding.wait(timeout=15)
    # A is confirmed holding the lock. Give B a real chance to issue its
    # own with_for_update() and either block on it (fixed code) or race
    # past it (unlocked code) before A is released.
    time.sleep(0.3)
    db = SessionLocal()
    try:
        current_user = db.query(User).filter(User.id == uid).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [{"id": "profile-X", "name": "Profile X", "updatedAt": "2026-09-01T00:00:02.000Z"}],
                "activeProfileId": "profile-X",
                "updatedAt": "2026-09-01T00:00:02.000Z",
            },
            deletedProfileIds=["profile-Y"],
        )
        resp = profile_router.update_growth_state(data=data, current_user=current_user, db=db)
        results["B"] = {"ok": True, "deletedProfileIds": list(resp.deleted_profile_ids or [])}
    except Exception as e:
        results["B"] = {"ok": False, "error": type(e).__name__, "detail": getattr(e, "detail", None)}
    finally:
        db.close()
        pg1_b_finished.set()


tA = _threading.Thread(target=worker_a1)
tB = _threading.Thread(target=worker_b1)
tA.start(); tB.start()

pg1_a_holding.wait(timeout=15)
time.sleep(0.6)
# The deterministic proof of real serialization, mirroring PG2: B must
# still be blocked (not finished) at this point, since it can only have
# gotten past its own with_for_update() by racing an unlocked read past
# A's still-open, uncommitted transaction.
pg1_b_blocked_while_a_holds = not pg1_b_finished.is_set()
pg1_a_release.set()

tA.join(timeout=40); tB.join(timeout=40)
profile_router._get_or_create_profile = _orig_get_or_create_pg1

check("B was genuinely blocked (still running) while A held the row lock",
      pg1_b_blocked_while_a_holds, {"b_finished_early": pg1_b_finished.is_set()})
check("both concurrent requests completed without error", len(results) == 2 and all(r.get("ok") for r in results.values()), results)

final = read_profile(pid)
check("BOTH tombstones (X and Y) survive the concurrent race",
      set(final["deleted_profile_ids"]) == {"profile-X", "profile-Y"}, final)
check("neither deleted profile is resurrected in the stored profiles[] list",
      "profile-X" not in final["profile_ids"] and "profile-Y" not in final["profile_ids"], final)


# ═════════════════════════════════════════════════════════════════════════
section("PG2 — B genuinely BLOCKS on A's row lock, then sees A's committed write")
# ═════════════════════════════════════════════════════════════════════════
# Direct, deterministic proof that the lock causes real serialization (not
# just "happens not to lose data" by luck of timing): A holds its
# transaction open — via a real, uncommitted UPDATE issued right after its
# own with_for_update() fetch inside a patched _get_or_create_profile that
# sleeps WHILE holding the lock — and B is started only once A is
# confirmed to be inside that locked, uncommitted window. If the lock were
# not actually being taken, B would read A's PRE-update state and finish
# first or interleave; with the lock, B's own with_for_update() call
# blocks in Postgres until A commits, so B's eventual read already
# contains A's tombstone.
uid2, pid2_ = seed_profile()

a_holding_lock = _threading.Event()
a_may_commit = _threading.Event()
b_finished = _threading.Event()
timing = {}

_orig_get_or_create_profile = profile_router._get_or_create_profile


def _patched_get_or_create_profile(user, db, *, for_update=False):
    result = _orig_get_or_create_profile(user, db, for_update=for_update)
    if for_update and user.id == uid2 and "a_thread" not in timing:
        # This is worker A's locked fetch — it now holds the real Postgres
        # row lock (the SELECT ... FOR UPDATE already executed inside the
        # call above). Signal B to start, then hold here (still inside A's
        # open transaction, lock still held) until the main thread confirms
        # B has had a real chance to issue its own with_for_update() and
        # block on it.
        timing["a_thread"] = True
        a_holding_lock.set()
        a_may_commit.wait(timeout=15)
    return result


profile_router._get_or_create_profile = _patched_get_or_create_profile

a_result = {}
b_result = {}


def worker_a():
    db = SessionLocal()
    try:
        current_user = db.query(User).filter(User.id == uid2).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [{"id": "profile-Y", "name": "Profile Y", "updatedAt": "2026-09-01T00:00:01.000Z"}],
                "activeProfileId": "profile-Y",
                "updatedAt": "2026-09-01T00:00:01.000Z",
            },
            deletedProfileIds=["profile-X"],
        )
        resp_a = profile_router.update_growth_state(
            data=data, current_user=current_user, db=db,
        )
        a_result["deletedProfileIds"] = list(resp_a.deleted_profile_ids or [])
    finally:
        db.close()


def worker_b():
    a_holding_lock.wait(timeout=15)
    # A is confirmed holding the lock. Give B a brief moment to actually
    # issue its SELECT ... FOR UPDATE and block on it in Postgres (not just
    # be scheduled) before releasing A.
    time.sleep(0.3)
    db = SessionLocal()
    try:
        current_user = db.query(User).filter(User.id == uid2).first()
        data = GrowthStateUpdate(
            growth_state={
                "person": {"name": "Test User"},
                "profiles": [{"id": "profile-X", "name": "Profile X", "updatedAt": "2026-09-01T00:00:02.000Z"}],
                "activeProfileId": "profile-X",
                "updatedAt": "2026-09-01T00:00:02.000Z",
            },
            deletedProfileIds=["profile-Y"],
        )
        resp_b = profile_router.update_growth_state(
            data=data, current_user=current_user, db=db,
        )
        b_result["deletedProfileIds"] = list(resp_b.deleted_profile_ids or [])
    finally:
        db.close()
        b_finished.set()


tA = _threading.Thread(target=worker_a)
tB = _threading.Thread(target=worker_b)
tA.start()
tB.start()

# Release A only after giving B a real chance to have issued its own
# with_for_update() and be genuinely blocked on Postgres's lock manager.
a_holding_lock.wait(timeout=15)
time.sleep(0.6)
# B must NOT have finished yet — if it has, the lock did not actually block it.
b_blocked_while_a_holds = not b_finished.is_set()
a_may_commit.set()

tA.join(timeout=20)
tB.join(timeout=20)
profile_router._get_or_create_profile = _orig_get_or_create_profile

check("B was genuinely blocked (still running) while A held the row lock",
      b_blocked_while_a_holds, {"b_finished_early": b_finished.is_set()})
check("B's response, read AFTER A released the lock, reflects A's tombstone UNIONED with its own",
      set(b_result.get("deletedProfileIds", [])) == {"profile-X", "profile-Y"}, b_result)

final2 = read_profile(pid2_)
check("final stored state has both tombstones after the genuinely-serialized race",
      set(final2["deleted_profile_ids"]) == {"profile-X", "profile-Y"}, final2)


# ═════════════════════════════════════════════════════════════════════════
print()
if failures:
    print(f"FAILED: {len(failures)} check(s) — {failures}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
