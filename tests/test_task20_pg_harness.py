"""
Task 20 — real-PostgreSQL concurrency proof for the DeliveryCycle claim
mechanism, the same pattern as test_task2_pg_harness.py / test_task19_pg_
harness.py (bootstrap duplicated per this repo's own established
convention — every PG-harness file inlines its own).

Why this exists alongside test_task20_durable_delivery.py (SQLite): that
file proves the state machine's LOGIC — every transition, every bounded
window, every Expo-outcome branch — using sequential simulated ticks.
What SQLite structurally cannot prove is real concurrency: `_try_claim`'s
`.with_for_update()` and `ensure_cycle_for_user`'s unique-constraint dedup
are the ONLY things standing between "two workers, one due user" and a
duplicate paid LLM call — the single highest-cost operation in this app.
This is deliberately narrow: it proves those two primitives under REAL
concurrent Postgres connections on real OS threads, not the whole state
machine again (already proven above).

    .venv/bin/python tests/test_task20_pg_harness.py

  PG1 — two real threads/connections racing `_try_claim` on the SAME
        DeliveryCycle row: exactly one succeeds, the other is refused —
        proving `.with_for_update()` genuinely serializes them (not just
        "no error was thrown").
  PG2 — the loser's thread, retried AFTER the winner finishes and releases
        the claim, correctly finds nothing left to do (state has moved on)
        — never re-claims and re-processes a cycle its rival already
        finished.
  PG3 — two real threads racing `ensure_cycle_for_user` for the SAME
        (user, day) at the same instant: exactly one row survives, proving
        `uq_delivery_cycle_user_date` — not application logic alone — is
        what prevents a duplicate cycle (and therefore duplicate
        generation) under a genuine race.
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
# Disposable cluster bootstrap — identical to test_task2/19_pg_harness.py
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
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _bootstrap_disposable_cluster() -> str:
    global _cluster_dir, _pg_started_here
    _cluster_dir = tempfile.mkdtemp(prefix="nibbler-pg-harness-task20-")
    data_dir = os.path.join(_cluster_dir, "data")
    sock_dir = os.path.join(_cluster_dir, "sock")
    os.makedirs(sock_dir, exist_ok=True)
    port = _free_port()

    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"

    try:
        subprocess.run(
            [_pg_bin("initdb"), "-D", data_dir, "-U", "postgres", "-A", "trust", "-E", "UTF8", "--no-sync"],
            check=True, capture_output=True, env=env, text=True,
        )
        subprocess.run(
            [_pg_bin("pg_ctl"), "-D", data_dir, "-o",
             f"-k {sock_dir} -h '' -p {port}", "-l", os.path.join(_cluster_dir, "pg.log"),
             "-w", "start"],
            check=True, capture_output=True, env=env, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"  [bootstrap] FAILED: {e}\n  stdout: {e.stdout}\n  stderr: {e.stderr}")
        raise
    _pg_started_here = True
    subprocess.run(
        [_pg_bin("psql"), "-h", sock_dir, "-p", str(port), "-U", "postgres", "-d", "postgres",
         "-c", "CREATE DATABASE nibbler_pg_harness_task20"],
        check=True, capture_output=True, env=env, text=True,
    )
    return f"postgresql://postgres@/nibbler_pg_harness_task20?host={sock_dir}&port={port}"


def _teardown_disposable_cluster():
    if not _cluster_dir:
        return
    if _pg_started_here:
        data_dir = os.path.join(_cluster_dir, "data")
        try:
            env = dict(os.environ); env["LC_ALL"] = "C"; env["LANG"] = "C"
            subprocess.run([_pg_bin("pg_ctl"), "-D", data_dir, "-m", "fast", "-w", "stop"],
                            capture_output=True, env=env, timeout=30)
        except Exception as e:
            print(f"  [teardown] pg_ctl stop raised: {e}")
    try:
        shutil.rmtree(_cluster_dir, ignore_errors=True)
    except Exception as e:
        print(f"  [teardown] cluster dir removal raised: {e}")
    print(f"  [teardown] disposable cluster stopped and removed ({_cluster_dir})")


external_url = os.environ.get("PG_HARNESS_DATABASE_URL")
if external_url:
    print("Using externally-provided disposable database (PG_HARNESS_DATABASE_URL set).")
    database_url = external_url
else:
    print("Bootstrapping a fully disposable local Postgres cluster...")
    atexit.register(_teardown_disposable_cluster)
    database_url = _bootstrap_disposable_cluster()
    print(f"  cluster ready at {database_url}")

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
os.environ["CLAUDE_API_KEY"] = "t"
os.environ["FIREBASE_PROJECT_ID"] = "test-project"
os.environ["SECRET_KEY"] = "test-secret-key-not-a-real-one-000000"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = database_url
_cwd_tempdir = tempfile.mkdtemp(prefix="nibbler-pg-harness-task20-cwd-")
os.chdir(_cwd_tempdir)
atexit.register(lambda: shutil.rmtree(_cwd_tempdir, ignore_errors=True))

import datetime  # noqa: E402

from app.database import create_tables, engine as db_engine, SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.delivery import DeliveryCycle  # noqa: E402
import app.services.delivery_lifecycle as dl  # noqa: E402

create_tables()

TODAY = datetime.date.today()
NOW = datetime.datetime.combine(TODAY, datetime.time(9, 0))


def db_factory():
    return SessionLocal()


try:
    # ═════════════════════════════════════════════════════════════════════
    section("PG1 — two REAL threads racing _try_claim on the SAME row: exactly one wins")
    # ═════════════════════════════════════════════════════════════════════
    db = db_factory()
    db.add(User(id="pguserA", email="pga@example.com"))
    db.commit()  # real Postgres enforces the FK — commit the parent row before the child, unlike SQLite
    cycle = DeliveryCycle(id="pgcycleA", user_id="pguserA", cycle_date=TODAY, state="due", due_at=NOW)
    db.add(cycle)
    db.commit()
    db.close()

    results = {}
    barrier = threading.Barrier(2)

    def _claimer(name, worker_id):
        barrier.wait()  # maximize the chance both threads hit with_for_update() at the same instant
        db = db_factory()
        claimed = dl._try_claim(db, "pgcycleA", worker_id, ("due",), NOW)
        results[name] = claimed is not None
        db.close()

    tA = threading.Thread(target=_claimer, args=("A", "worker-A"))
    tB = threading.Thread(target=_claimer, args=("B", "worker-B"))
    tA.start(); tB.start()
    tA.join(timeout=15); tB.join(timeout=15)

    check("both threads completed (no deadlock/hang)", "A" in results and "B" in results, results)
    check("EXACTLY one of the two real concurrent claims succeeded",
          sorted([results.get("A"), results.get("B")]) == [False, True], results)

    db = db_factory()
    final = db.query(DeliveryCycle).filter(DeliveryCycle.id == "pgcycleA").first()
    check("the row is claimed by exactly one real worker id (A or B, not both/neither)",
          final.claimed_by in ("worker-A", "worker-B"), final.claimed_by)
    winner = final.claimed_by
    db.close()

    # ═════════════════════════════════════════════════════════════════════
    section("PG2 — the loser, retried AFTER the winner finishes, correctly finds nothing left to do")
    # ═════════════════════════════════════════════════════════════════════
    # Winner finishes the cycle (simulating real work completing).
    db = db_factory()
    row = db.query(DeliveryCycle).filter(DeliveryCycle.id == "pgcycleA").first()
    row.state = "push_pending"
    row.claimed_by = None
    row.claimed_until = None
    db.commit()
    db.close()

    loser_worker = "worker-B" if winner == "worker-A" else "worker-A"
    db = db_factory()
    late_claim = dl._try_claim(db, "pgcycleA", loser_worker, ("due",), NOW)
    db.close()
    check("the loser's late retry against the now-moved-on row correctly finds it unclaimable "
          "(state is no longer 'due') — never reprocesses a cycle its rival already finished",
          late_claim is None)

    # ═════════════════════════════════════════════════════════════════════
    section("PG3 — two REAL threads racing ensure_cycle_for_user for the SAME (user, day)")
    # ═════════════════════════════════════════════════════════════════════
    db = db_factory()
    db.add(User(id="pguserC", email="pgc@example.com"))
    db.commit()
    db.close()

    created_ids = {}
    barrier2 = threading.Barrier(2)

    def _creator(name):
        barrier2.wait()
        db = db_factory()
        c = dl.ensure_cycle_for_user(db, "pguserC", TODAY, NOW, f"creator-{name}")
        created_ids[name] = c.id if c else None
        db.close()

    tC = threading.Thread(target=_creator, args=("X",))
    tD = threading.Thread(target=_creator, args=("Y",))
    tC.start(); tD.start()
    tC.join(timeout=15); tD.join(timeout=15)

    check("both concurrent creators completed", "X" in created_ids and "Y" in created_ids, created_ids)
    check("both concurrent creators observed the SAME winning row id "
          "(uq_delivery_cycle_user_date, not luck, prevented a duplicate)",
          created_ids.get("X") == created_ids.get("Y") and created_ids.get("X") is not None,
          created_ids)

    db = db_factory()
    row_count = db.query(DeliveryCycle).filter(
        DeliveryCycle.user_id == "pguserC", DeliveryCycle.cycle_date == TODAY
    ).count()
    db.close()
    check("exactly ONE DeliveryCycle row exists for (pguserC, today) after the real race",
          row_count == 1, row_count)

finally:
    _teardown_disposable_cluster()
    atexit.unregister(_teardown_disposable_cluster)

print(f"\n{'='*60}")
if failures:
    print(f"FAILED: {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
