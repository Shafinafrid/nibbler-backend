"""
Real-PostgreSQL proof for finding #5's session-generation claim/lease.

Same bootstrap pattern as tests/test_personalization_pg_harness.py /
test_task2_pg_harness.py / test_task20_pg_harness.py — a fully disposable
local Postgres cluster, short temp-dir prefix (Unix socket path is capped at
103 bytes).

  PG1 — two REAL sessions drive the ACTUAL endpoint function
        (bites.get_or_create_session) concurrently for the same
        (user, item, date), with a barrier holding both inside the mocked
        slow LLM call so their claims genuinely overlap. The LLM call
        boundary is invoked exactly once; both callers converge on the same
        canonical session id; a loser (if any) gets the retryable
        session_generating 409 and a real retry against the same endpoint
        converges on the winner's exact row — never re-triggering the LLM.
  PG2 — a superseded worker (lease expired, another worker legitimately
        took over) cannot finalize over the new owner, on real Postgres
        with two real sessions (not SQLite's more forgiving identity-map
        behavior — see PersonalizationQuestion round-3's history for why
        this distinction mattered there).
  PG3 — the scheduler's DeliveryCycle-claimed generation phase
        (delivery_lifecycle.process_generation_phase) and an HTTP request
        racing for the SAME (user, item, date) slot converge through this
        ONE primitive: exactly one LLM call, one row, both callers agree.

    .venv/bin/python tests/test_session_generation_pg_harness.py
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
    _cluster_dir = tempfile.mkdtemp(prefix="nib-sg-")
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
         "-c", "CREATE DATABASE nibbler_pg_session_gen"],
        check=True, capture_output=True, env=env, text=True)
    return f"postgresql://postgres@/nibbler_pg_session_gen?host={sock_dir}&port={port}"


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
from sqlalchemy import text as sa_text

from app.database import create_tables, engine, SessionLocal
from app.models.user import User
from app.models.library import LibraryItem
from app.models.bite import DailyBite
from app.services import session_service as ss
from app.services.session_service import generate_session_for_item, SessionGenerationError
from app.routers import bites as bites_router

# create_tables() runs the REAL migration sequence (including the
# uq_daily_bites_user_item_date partial unique index this whole claim
# mechanism depends on) plus verify_required_schema() — a stronger
# guarantee than a bare Base.metadata.create_all() would give, and proves
# the claimed_by/claimed_until columns this session's fix added are
# actually reachable through the real boot path, not just the ORM model.
create_tables()

TODAY = datetime.date.today()


def _fast_deck():
    return {
        "title": "Session Title", "chapter": "Ch 1", "headline": "Headline.",
        "preview": "Preview.",
        "cards": [
            {"kind": "hook", "eyebrow": "TODAY'S SESSION", "title": "Hook",
             "body": "Body.", "highlight": None, "options": None, "explanation": None},
            {"kind": "summary", "eyebrow": "SESSION SUMMARY", "title": "Summary",
             "body": "Body.", "highlight": None, "options": None, "explanation": None},
        ],
        "quiz": None,
    }


def seed(tag):
    uid = "u-" + tag + "-" + uuid.uuid4().hex[:8]
    iid = "i-" + tag + "-" + uuid.uuid4().hex[:8]
    s = SessionLocal()
    s.add(User(id=uid, email=f"{uid}@t.test", is_premium=True))
    s.commit()
    s.add(LibraryItem(
        id=iid, user_id=uid, title="Atomic Habits", type="text",
        content="A distinctive passage about compounding small habits. " * 40,
        processed=True, mode="wisdom", is_active=True,
    ))
    s.commit()
    s.close()
    return uid, iid


def db_rows(uid, iid):
    s = SessionLocal()
    try:
        rows = s.query(DailyBite).filter(
            DailyBite.user_id == uid, DailyBite.library_item_id == iid,
        ).all()
        return [{"id": r.id, "cards": bool(r.cards), "claimed_by": r.claimed_by} for r in rows]
    finally:
        s.close()


# ═════════════════════════════════════════════════════════════════════════
section("PG1 — two REAL sessions racing the ACTUAL endpoint for one slot")
# ═════════════════════════════════════════════════════════════════════════
uid, iid = seed("ep1")

barrier = _threading.Barrier(2, timeout=30)
released = _threading.Event()
call_count = {"n": 0}
call_lock = _threading.Lock()
results = {}


def _slow_generate_wisdom_session(self, **kwargs):
    with call_lock:
        call_count["n"] += 1
    try:
        barrier.wait()
    except Exception:
        pass
    released.wait(timeout=10)
    return _fast_deck()


ss.LLMService.generate_wisdom_session = _slow_generate_wisdom_session


class _FakeUser:
    def __init__(self, uid): self.id = uid


class _Req:  # slowapi/limiter shim, matching the personalization PG harness
    class _C: host = "127.0.0.1"
    client = _C()
    headers = {}
    class _S: user_id = None
    state = _S()


class _BG:  # BackgroundTasks shim — the endpoint schedules a Mixpanel track
    def add_task(self, *a, **kw): pass


def worker(label):
    db = SessionLocal()
    try:
        current_user = db.query(User).filter(User.id == uid).first()
        data = bites_router.SessionRequest(library_item_id=iid, read_length=5)
        resp = bites_router.get_or_create_session.__wrapped__(
            request=_Req(), data=data, background_tasks=_BG(),
            current_user=current_user, db=db,
        )
        results[label] = {"ok": True, "id": resp.id, "cards": len(resp.cards)}
    except Exception as e:
        results[label] = {"ok": False, "error": type(e).__name__,
                          "detail": getattr(e, "detail", None)}
    finally:
        db.close()


tA = _threading.Thread(target=worker, args=("A",))
tB = _threading.Thread(target=worker, args=("B",))
tA.start(); tB.start()
_threading.Timer(1.0, released.set).start()
tA.join(timeout=40); tB.join(timeout=40)

check("both concurrent requests completed", len(results) == 2, results)
check("generate_wisdom_session was invoked EXACTLY ONCE across the real race (real Postgres)",
      call_count["n"] == 1, call_count)

rows = db_rows(uid, iid)
check("exactly ONE DailyBite row exists for the slot", len(rows) == 1, rows)
check("that row is fully generated and unclaimed",
      len(rows) == 1 and rows[0]["cards"] and rows[0]["claimed_by"] is None, rows)

succeeded = [r for r in results.values() if r.get("ok")]
check("every successful response's session id matches the single canonical row",
      len(rows) == 1 and all(r["id"] == rows[0]["id"] for r in succeeded),
      {"canonical": rows[0]["id"] if rows else None, "results": results})

losers = [(l, r) for l, r in results.items() if not r.get("ok")]
check("at most one loser, and if present it's the retryable session_generating 409",
      len(losers) in (0, 1) and (not losers or (
          losers[0][1].get("error") == "HTTPException"
          and isinstance(losers[0][1].get("detail"), dict)
          and losers[0][1]["detail"].get("code") == "session_generating"
      )),
      results)

if losers:
    loser_label, _ = losers[0]
    retry_db = SessionLocal()
    try:
        current_user = retry_db.query(User).filter(User.id == uid).first()
        data = bites_router.SessionRequest(library_item_id=iid, read_length=5)
        retry_resp = bites_router.get_or_create_session.__wrapped__(
            request=_Req(), data=data, background_tasks=_BG(),
            current_user=current_user, db=retry_db,
        )
        retry_ok, retry_id = True, retry_resp.id
    except Exception as e:
        retry_ok, retry_id = False, None
    finally:
        retry_db.close()
    check("the loser's retry succeeds and converges on the SAME canonical session id",
          retry_ok and retry_id == rows[0]["id"], {"retry_id": retry_id, "canonical": rows[0]["id"]})
    check("the retry did not trigger a second LLM call",
          call_count["n"] == 1, call_count)
else:
    check("(no loser this run — both requests converged on the answered-row path)", True)


# ═════════════════════════════════════════════════════════════════════════
section("PG2 — a superseded worker cannot finalize over the new owner (real Postgres)")
# ═════════════════════════════════════════════════════════════════════════
uid2, iid2 = seed("ep2")
sA, sB = SessionLocal(), SessionLocal()

claimed_a, won_a = ss._claim_or_find_daily_bite(sA, uid2, iid2, TODAY, "worker-A")
check("worker A claims the slot", won_a is True)

exp = SessionLocal()
exp.execute(sa_text(
    "UPDATE daily_bites SET claimed_until = :t WHERE user_id = :u AND library_item_id = :i"),
    {"t": datetime.datetime.utcnow() - datetime.timedelta(minutes=10), "u": uid2, "i": iid2})
exp.commit(); exp.close()

claimed_b, won_b = ss._claim_or_find_daily_bite(sB, uid2, iid2, TODAY, "worker-B")
check("worker B takes over the expired lease (real Postgres, real 2nd session)", won_b is True)
check("worker B's row is the SAME row A originally claimed (re-claimed, not a duplicate)",
      claimed_b is not None and claimed_a is not None and claimed_b.id == claimed_a.id,
      {"a": claimed_a.id if claimed_a else None, "b": claimed_b.id if claimed_b else None})

# A finishes late, in its own session which still thinks it owns the claim.
ok_a = ss._finalize_daily_bite_claim(sA, claimed_a.id, "worker-A", {
    "title": "A-late", "insight": "", "reflection": "", "action": "", "source": "s",
    "theme": "wisdom", "cards": [{"kind": "summary"}], "quiz": None, "read_length": 5,
    "mode": "wisdom", "chapter": "", "headline": "", "preview": "", "goal_passage": None,
    "chunk_ids": None, "origin": "manual",
})
check("the SUPERSEDED worker A cannot finalize (real Postgres, real 2nd session)", ok_a is False)

ok_b = ss._finalize_daily_bite_claim(sB, claimed_b.id, "worker-B", {
    "title": "B-wins", "insight": "", "reflection": "", "action": "", "source": "s",
    "theme": "wisdom", "cards": [{"kind": "summary"}], "quiz": None, "read_length": 5,
    "mode": "wisdom", "chapter": "", "headline": "", "preview": "", "goal_passage": None,
    "chunk_ids": None, "origin": "manual",
})
check("the OWNING worker B finalizes successfully", ok_b is True)

rows2 = db_rows(uid2, iid2)
check("the database records B's title, not A's discarded late write",
      len(rows2) == 1, rows2)
final2 = SessionLocal()
title2 = final2.query(DailyBite).filter(DailyBite.id == claimed_b.id).first().title
final2.close()
check("the persisted title is the OWNING worker's, never the superseded one's",
      title2 == "B-wins", title2)

sA.close(); sB.close()


# ═════════════════════════════════════════════════════════════════════════
section("PG3 — an HTTP request and the scheduler's generation phase converge on ONE call")
# ═════════════════════════════════════════════════════════════════════════
uid3, iid3 = seed("ep3")

call_count["n"] = 0
barrier3 = _threading.Barrier(2, timeout=30)
released3 = _threading.Event()


def _slow_generate_wisdom_session_3(self, **kwargs):
    with call_lock:
        call_count["n"] += 1
    try:
        barrier3.wait()
    except Exception:
        pass
    released3.wait(timeout=10)
    return _fast_deck()


ss.LLMService.generate_wisdom_session = _slow_generate_wisdom_session_3

results3 = {}


def http_worker():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid3).first()
        item = db.query(LibraryItem).filter(LibraryItem.id == iid3).first()
        bite = generate_session_for_item(
            db, user=user, item=item, read_length=5, profile={}, today=TODAY, origin="manual",
        )
        results3["http"] = {"ok": True, "id": bite.id}
    except SessionGenerationError as e:
        results3["http"] = {"ok": False, "code": e.code}
    finally:
        db.close()


def scheduler_worker():
    """Simulates delivery_lifecycle.process_generation_phase's own call
    into generate_session_for_item — same function, same claim, proving
    the primitive covers BOTH callers without either needing its own
    separate lock."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid3).first()
        item = db.query(LibraryItem).filter(LibraryItem.id == iid3).first()
        bite = generate_session_for_item(
            db, user=user, item=item, read_length=5, profile={}, today=TODAY, origin="scheduled",
        )
        results3["scheduler"] = {"ok": True, "id": bite.id}
    except SessionGenerationError as e:
        results3["scheduler"] = {"ok": False, "code": e.code}
    finally:
        db.close()


tH = _threading.Thread(target=http_worker)
tS = _threading.Thread(target=scheduler_worker)
tH.start(); tS.start()
_threading.Timer(1.0, released3.set).start()
tH.join(timeout=40); tS.join(timeout=40)

check("both the HTTP-path and scheduler-path callers completed", len(results3) == 2, results3)
check("generate_wisdom_session was invoked EXACTLY ONCE even though the callers are DIFFERENT code paths",
      call_count["n"] == 1, call_count)

rows3 = db_rows(uid3, iid3)
check("exactly ONE row exists for the slot regardless of which caller generated it", len(rows3) == 1, rows3)
succeeded3 = [r for r in results3.values() if r.get("ok")]
check("every successful caller's id matches the single canonical row",
      len(rows3) == 1 and all(r["id"] == rows3[0]["id"] for r in succeeded3),
      {"canonical": rows3[0]["id"] if rows3 else None, "results": results3})


print()
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: all session-generation PostgreSQL harness checks passed")
