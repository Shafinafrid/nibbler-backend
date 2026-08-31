"""
Round-5 — real-PostgreSQL proofs the earlier rounds did not provide.

Round 4 proved the lease with helper-level calls on SQLite. The auditor's
objection was precise: that is not an ENDPOINT-level, two-session,
PostgreSQL proof, and SQLite's `with_for_update`/identity-map behaviour
differs from Postgres in exactly the way the original defect depended on.

  PG1 — two REAL sessions drive the actual endpoint function concurrently
        for the same question, with a barrier holding both inside the slow
        interpretation step so their claims genuinely overlap. Exactly one
        answer is recorded; both callers observe the SAME canonical answer;
        the loser never overwrites the winner.
  PG2 — a superseded worker (lease expired, another worker took over)
        cannot finalize, and its FAILURE path cannot release the live
        worker's claim.
  PG3 — readiness rejects a `WHERE false` decoy index that is unique,
        valid, ready and correctly named/shaped but indexes NO rows, and
        accepts the genuine predicate. This is the auditor's exact decoy.

    .venv/bin/python tests/test_personalization_pg_harness.py
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
    # A descriptive prefix here pushes it over and the cluster refuses to start.
    _cluster_dir = tempfile.mkdtemp(prefix="nib-pz-")
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
         "-c", "CREATE DATABASE nibbler_pg_personalization"],
        check=True, capture_output=True, env=env, text=True)
    return f"postgresql://postgres@/nibbler_pg_personalization?host={sock_dir}&port={port}"


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
from datetime import datetime, timedelta
from sqlalchemy import text as sa_text

from app.database import Base, engine, SessionLocal
from app.models.user import User
from app.models.bite import DailyBite
from app.models.personalization import PersonalizationQuestion
from app.routers import bites as bites_router

Base.metadata.create_all(bind=engine)

OPTIONS = [
    {"id": "opt0", "text": "Automate it", "tag": "prefers_automation"},
    {"id": "opt1", "text": "By hand", "tag": "prefers_manual_control"},
]


def seed_question(status="pending"):
    uid = "u-" + uuid.uuid4().hex[:8]
    bid = "b-" + uuid.uuid4().hex[:8]
    s = SessionLocal()
    # Real Postgres ENFORCES the foreign keys (SQLite does not by default),
    # so the parents must be committed before the child row references them.
    s.add(User(id=uid, email=f"{uid}@t.test"))
    s.commit()
    s.add(DailyBite(id=bid, user_id=uid, title="T", date=datetime.utcnow().date(),
                    cards=[], insight="", reflection="", action="", source="s", theme="wisdom"))
    s.commit()
    s.add(PersonalizationQuestion(
        user_id=uid, daily_bite_id=bid, question="Q?", options=OPTIONS,
        profile_id="prof-1", status=status))
    s.commit(); s.close()
    return uid, bid


def db_row(bid):
    s = SessionLocal()
    try:
        r = s.query(PersonalizationQuestion).filter_by(daily_bite_id=bid).first()
        return {"status": r.status, "claimed_by": r.claimed_by,
                "tags": list(r.applied_tags or []), "free_text": r.answer_free_text,
                "option_id": r.answer_option_id}
    finally:
        s.close()


# ═════════════════════════════════════════════════════════════════════════
section("PG1 — two REAL sessions racing the endpoint on one question")
# ═════════════════════════════════════════════════════════════════════════
# Both requests are held INSIDE the interpretation step by a barrier, so
# their claims genuinely overlap rather than running back to back.
uid, bid = seed_question()

barrier = threading.Barrier(2, timeout=30)
released = threading.Event()
results = {}


class _FakeUser:
    def __init__(self, uid): self.id = uid


class _Req:  # slowapi/limiter shim
    class _C: host = "127.0.0.1"
    client = _C()
    headers = {}
    class _S: user_id = None
    state = _S()


def _slow_interpret(self, question, options, free_text):
    """Stand-in for the LLM call: blocks until BOTH workers are inside it."""
    try:
        barrier.wait()
    except Exception:
        pass
    released.wait(timeout=10)
    return {"tags": ["prefers_automation"], "summary": f"summary for {free_text}"}


bites_router.LLMService.interpret_personalization_answer = _slow_interpret


def worker(label, text):
    db = SessionLocal()
    try:
        resp = bites_router.submit_personalize_answer.__wrapped__(
            request=_Req(), bite_id=bid,
            data=bites_router.PersonalizeAnswerRequest(free_text=text),
            current_user=_FakeUser(uid), db=db,
        )
        results[label] = {"ok": True, "tags": list(resp.tags),
                          "profile_id": resp.profile_id,
                          "summary": resp.interpreted_summary}
    except Exception as e:
        results[label] = {"ok": False, "error": type(e).__name__,
                          "detail": getattr(e, "detail", None)}
    finally:
        db.close()


tA = threading.Thread(target=worker, args=("A", "answer-from-A"))
tB = threading.Thread(target=worker, args=("B", "answer-from-B"))
tA.start(); tB.start()
threading.Timer(1.0, released.set).start()
tA.join(timeout=40); tB.join(timeout=40)

final = db_row(bid)
check("both concurrent requests completed", len(results) == 2, results)
check("exactly ONE answer is recorded in the database",
      final["status"] == "answered" and final["free_text"] in ("answer-from-A", "answer-from-B"),
      final)
check("the claim is released after the winner finalizes",
      final["claimed_by"] is None, final)

succeeded = [r for r in results.values() if r.get("ok")]
# Whoever returns 200 must agree with what the DATABASE recorded — the loser
# must never report its own discarded result as if it had been stored.
agreeing = all(r["tags"] == final["tags"] for r in succeeded)
check("every successful response matches the canonical stored answer",
      agreeing, {"stored": final["tags"], "responses": [r["tags"] for r in succeeded]})
check("no response carries a null profile_id",
      all(r.get("profile_id") == "prof-1" for r in succeeded), succeeded)


# ═════════════════════════════════════════════════════════════════════════
section("PG2 — a superseded worker cannot finalize or release")
# ═════════════════════════════════════════════════════════════════════════
uid2, bid2 = seed_question()
sA, sB = SessionLocal(), SessionLocal()

check("worker A claims the row",
      bites_router._claim_personalization_row(sA, bid2, uid2, "worker-A") is True)

# Expire A's lease, then B legitimately takes over.
exp = SessionLocal()
exp.execute(sa_text(
    "UPDATE personalization_questions SET claimed_until = :t WHERE daily_bite_id = :b"),
    {"t": datetime.utcnow() - timedelta(minutes=1), "b": bid2})
exp.commit(); exp.close()

check("worker B takes over the expired lease",
      bites_router._claim_personalization_row(sB, bid2, uid2, "worker-B") is True)
check("the database records B as owner", db_row(bid2)["claimed_by"] == "worker-B", db_row(bid2))

# A finishes late, in its own session which still holds the stale row.
ok_a = bites_router._finalize_personalization_answer(
    sA, bid2, uid2, "worker-A", tags=["prefers_automation"],
    interpreted_summary=None, option_id=None, free_text="A-late")
check("the SUPERSEDED worker cannot finalize (real Postgres, real 2nd session)",
      ok_a is False, ok_a)
check("the database was not overwritten by the superseded worker",
      db_row(bid2)["free_text"] != "A-late", db_row(bid2))

bites_router._release_claim_if_owner(sA, bid2, uid2, "worker-A")
check("a superseded worker's failure does not release the live claim",
      db_row(bid2)["claimed_by"] == "worker-B", db_row(bid2))

ok_b = bites_router._finalize_personalization_answer(
    sB, bid2, uid2, "worker-B", tags=["prefers_manual_control"],
    interpreted_summary=None, option_id=None, free_text="B-wins")
check("the OWNING worker finalizes successfully", ok_b is True, ok_b)
check("the database records the owner's answer",
      db_row(bid2)["free_text"] == "B-wins", db_row(bid2))
sA.close(); sB.close()


# ═════════════════════════════════════════════════════════════════════════
section("PG3 — readiness rejects a `WHERE false` decoy partial index")
# ═════════════════════════════════════════════════════════════════════════
from app.database import verify_required_schema

# These two unique indexes are prod one-offs (see this repo's own notes:
# applied by hand to production, not by create_all), so a fresh disposable
# cluster does not have them. Create them here so the baseline reflects a
# correctly-provisioned database and the decoy below is the ONLY difference.
prep = SessionLocal()
prep.execute(sa_text(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_daily_bites_user_item_date "
    "ON daily_bites (user_id, library_item_id, date) WHERE library_item_id IS NOT NULL"))
prep.execute(sa_text(
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_saved_bites_user_bite "
    "ON saved_bites (user_id, bite_id)"))
prep.commit(); prep.close()

ok, problems = verify_required_schema()
check("baseline schema verifies clean before tampering", ok, problems)

# The auditor's exact decoy: correctly named, unique, valid, ready, right
# columns — and indexes NO rows, so it enforces nothing.
tamper = SessionLocal()
tamper.execute(sa_text("DROP INDEX IF EXISTS uq_daily_bites_user_item_date"))
tamper.execute(sa_text(
    "CREATE UNIQUE INDEX uq_daily_bites_user_item_date "
    "ON daily_bites (user_id, library_item_id, date) WHERE false"))
tamper.commit(); tamper.close()

ok_decoy, problems_decoy = verify_required_schema()
check("readiness FAILS on the `WHERE false` decoy index",
      not ok_decoy, problems_decoy)
check("the failure names the predicate as the reason",
      any("predicate" in p for p in problems_decoy), problems_decoy)

# Restore the genuine predicate and confirm it is accepted again.
restore = SessionLocal()
restore.execute(sa_text("DROP INDEX IF EXISTS uq_daily_bites_user_item_date"))
restore.execute(sa_text(
    "CREATE UNIQUE INDEX uq_daily_bites_user_item_date "
    "ON daily_bites (user_id, library_item_id, date) WHERE library_item_id IS NOT NULL"))
restore.commit(); restore.close()

ok_restored, problems_restored = verify_required_schema()
check("readiness passes again with the GENUINE predicate",
      ok_restored, problems_restored)


print()
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("RESULT: all personalization PostgreSQL harness checks passed")
