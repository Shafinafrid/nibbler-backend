"""
Task 20 — durable daily nibble generation + notification delivery, coordinated
with Tasks 3, 4, 15, 16 and 19.

Investigation (see the Task 20 design report) confirmed the exact gap: the
5-minute scheduler was a stateless query-and-act pass with no durable record
that a user's daily cycle was ever owed, so a restart between "due" and
"pushed" silently lost that day's cycle until the same slot recurred 24h
later, and Expo's per-message ticket outcome was parsed then discarded —
success/failure/timeout were indistinguishable. `app/services/delivery_
lifecycle.py` fixes this with a durable `DeliveryCycle` ledger + a bounded
reconciliation sweep, additive to (not replacing) the existing on-time
scheduler passes.

No real AI or Expo call is made anywhere in this file — `generate_session_
for_item` and `send_push_messages` are both monkeypatched module attributes,
matching this repo's established convention (see test_task3_scheduler_
integration.py). Every cycle is re-queried fresh within its own short-lived
session right before use (never an ORM object carried across sessions) —
`SessionLocal` defaults to `expire_on_commit=True`, so an object touched by
an earlier commit is unsafe to read after that session closes.

  A — happy path: due -> generated (exactly one AI call) -> push_pending ->
      sent (ticket 'ok') -> completed.
  B — restart recovery, generation phase: a cycle claimed by a "dead"
      worker (lease already expired) is safely reclaimed and finished by
      the next reconcile pass — with exactly one AI call, not two. A
      genuinely LIVE lease is correctly left alone.
  C — restart recovery, push phase: a cycle already at push_pending when
      the "process" restarts still gets its push sent on the next
      reconcile pass, with ZERO additional AI calls.
  D — concurrent/duplicate ticks: two overlapping reconcile passes against
      the SAME due cycle produce exactly one AI call and one canonical
      DailyBite — the claim lease, not luck, prevents the duplicate.
  E — unread-hold: an existing unread bite blocks generation (held_unread,
      zero AI calls); once read, the NEXT reconcile pass generates.
  F — Expo outcome differentiation: ticket 'ok' -> completed; ticket
      DeviceNotRegistered -> terminal_failure (dead-letter, no retry);
      transport failure (no tickets) -> push_submitted_unknown, retried,
      NEVER regenerates; MAX_ATTEMPTS exhausted -> terminal_failure.
  G — bounded catch-up: a cycle whose due_at is already past
      GENERATION_CATCHUP_WINDOW is marked window_expired without ever
      calling the LLM.
  H — notification disabled / no live token: a push_pending cycle with no
      enabled token completes without attempting a send (nothing owed).
  I — no eligible sources (deleted/locked): a due cycle finds zero active
      sources -> terminal_failure, zero AI calls — no stale content risk.
  J — DeliveryCycle.user_id is a real ON DELETE CASCADE FK (matching
      ChatTurn's already-shipped, already-proven pattern) — account
      deletion cannot leave orphaned cycles.
  K — streak-alert bounded catch-up + idempotency: an exact-slot match
      fires; a match within the 20-minute catch-up window still fires
      (recovers a missed tick); a match past the window does NOT fire
      (never a stale "ends soon" alert); the SAME window match on a
      second tick does not double-send.
  L — regression, window_expired ACTUALLY PERSISTS for a never-claimed
      row: an independent audit found the original code called `_finish`
      before claiming, which silently no-ops (only the current claim
      owner may write) — so 'window_expired' was returned to the caller
      but the DB row stayed 'due' forever, re-evaluated identically on
      every future tick. Fixed by claiming first; this proves the fix.
  M — regression, the production worker id used by the real scheduler is
      genuinely unique per process (not the literal "scheduler" an
      earlier version hardcoded, which made the claim lease's cross-
      process protection a complete no-op — two processes sharing one
      worker_id could both "win" a claim on the same live row).
  N — regression, streak alert fires correctly for a delivery time in the
      00:00-00:40 UTC band: an independent audit found the windowed
      match's `slot_dt` construction combined the T-65 hour/minute onto
      `now`'s OWN calendar date, which is off by one day whenever the
      alert slot for an early-UTC delivery time actually falls on the
      PREVIOUS date — making the alert permanently, silently unfireable
      for that whole band, on any tick, ever. Fixed by trying the
      adjacent calendar day on both sides.
"""
import os
import sys
import tempfile
import datetime
import uuid
import asyncio

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/task20.db", FIREBASE_PROJECT_ID="t")
sys.path.insert(0, BACKEND)
import hermetic  # noqa: F401 — must precede `app.` imports

failures = []
def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  — {detail}" if detail else ""))
    if not cond: failures.append(name)
def section(t): print(f"\n=== {t} ===")

from app.database import create_tables, SessionLocal  # noqa: E402
from app.models.user import User  # noqa: E402
from app.models.push_token import PushToken  # noqa: E402
from app.models.library import LibraryItem  # noqa: E402
from app.models.bite import DailyBite  # noqa: E402
from app.models.streak import Streak  # noqa: E402
from app.models.delivery import DeliveryCycle  # noqa: E402
import app.services.notification_service as notif_service  # noqa: E402
import app.services.session_service as session_service  # noqa: E402
import app.services.delivery_lifecycle as dl  # noqa: E402

create_tables()


def db_factory():
    return SessionLocal()


TODAY = datetime.date.today()
NOW = datetime.datetime.combine(TODAY, datetime.time(9, 0))  # naive UTC, matches the delivery_cycles columns

# ── Fakes (never call real AI or Expo — matches this repo's convention) ────
gen_calls = []
gen_should_fail = {"flag": False}


def _fake_generate_session_for_item(db, user, item, read_length, profile, today, origin):
    gen_calls.append((user.id, item.id, today))
    if gen_should_fail["flag"]:
        raise session_service.SessionGenerationError("forced test failure")
    bite = DailyBite(
        id=f"bite-{uuid.uuid4()}", user_id=user.id, library_item_id=item.id,
        title="t", insight="i", reflection="r", action="a", date=today,
        headline=f"Hook for {item.title}", origin=origin, source=item.title,
        generated_at=datetime.datetime.utcnow(),
    )
    db.add(bite)
    db.commit()
    db.refresh(bite)
    return bite


session_service.generate_session_for_item = _fake_generate_session_for_item

push_calls = []
push_tickets_to_return = {"tickets": [{"status": "ok"}]}


async def _fake_send_push_messages(messages, expo_access_token=""):
    push_calls.append(list(messages))
    return list(push_tickets_to_return["tickets"])


notif_service.send_push_messages = _fake_send_push_messages


def mkuser(uid, active_book=True, token=True):
    db = db_factory()
    db.add(User(id=uid, email=f"{uid}@example.com"))
    if active_book:
        db.add(LibraryItem(id=f"{uid}-book", user_id=uid, title=f"{uid}'s Book", type="pdf",
                            processed=True, is_active=True, deletion_state=None))
    if token:
        db.add(PushToken(id=str(uuid.uuid4()), user_id=uid, token=f"ExponentPushToken[{uid}]",
                          notifications_enabled=True))
    db.commit()
    db.close()


def get_cycle_state(uid, cdate=TODAY):
    """Read-only snapshot of one cycle's fields as plain Python values —
    safe to use anywhere, never passed back into a mutating call."""
    db = db_factory()
    c = db.query(DeliveryCycle).filter(DeliveryCycle.user_id == uid, DeliveryCycle.cycle_date == cdate).first()
    if c is None:
        db.close()
        return None
    snap = {
        "id": c.id, "state": c.state, "attempts": c.attempts, "last_error": c.last_error,
        "daily_bite_id": c.daily_bite_id, "claimed_by": c.claimed_by, "claimed_until": c.claimed_until,
    }
    db.close()
    return snap


def create_cycle(uid, cdate=TODAY, due_at=NOW, worker="setup"):
    db = db_factory()
    dl.ensure_cycle_for_user(db, uid, cdate, due_at, worker)
    db.close()
    return get_cycle_state(uid, cdate)["id"]


def run_generation(cycle_id, worker_id, now):
    """Fetch the cycle fresh in its own session, run the generation phase,
    return the phase's result string. Never carries the ORM object out."""
    db = db_factory()
    c = db.query(DeliveryCycle).filter(DeliveryCycle.id == cycle_id).first()
    result = dl.process_generation_phase(db, c, worker_id, now)
    db.close()
    return result


def run_push(cycle_id, worker_id, now):
    db = db_factory()
    c = db.query(DeliveryCycle).filter(DeliveryCycle.id == cycle_id).first()
    result = dl.process_push_phase(db, c, worker_id, now)
    db.close()
    return result


def try_claim(cycle_id, worker_id, expected_states, now):
    db = db_factory()
    claimed = dl._try_claim(db, cycle_id, worker_id, expected_states, now)
    ok = claimed is not None
    db.close()
    return ok


def set_cycle_fields(cycle_id, **fields):
    db = db_factory()
    c = db.query(DeliveryCycle).filter(DeliveryCycle.id == cycle_id).first()
    for k, v in fields.items():
        setattr(c, k, v)
    db.commit()
    db.close()


def bite_count(uid, cdate=TODAY):
    db = db_factory()
    n = db.query(DailyBite).filter(DailyBite.user_id == uid, DailyBite.date == cdate).count()
    db.close()
    return n


# ═════════════════════════════════════════════════════════════════════════
section("A — happy path: due -> generated (1 AI call) -> push sent -> completed")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear(); push_calls.clear()
mkuser("userA")
cid = create_cycle("userA")
check("cycle created in 'due' state", get_cycle_state("userA")["state"] == "due")

result = run_generation(cid, "test-worker", NOW)
check("generation phase result is push_pending", result == "push_pending", result)
check("exactly ONE AI generation call", len(gen_calls) == 1, len(gen_calls))

snap = get_cycle_state("userA")
check("cycle now push_pending with a daily_bite_id set",
      snap["state"] == "push_pending" and snap["daily_bite_id"], snap)

push_tickets_to_return["tickets"] = [{"status": "ok"}]
result2 = run_push(cid, "test-worker", NOW)
check("push phase result is completed", result2 == "completed", result2)
check("exactly one push batch sent", len(push_calls) == 1, len(push_calls))
snap = get_cycle_state("userA")
check("cycle terminal state is completed, claim released",
      snap["state"] == "completed" and snap["claimed_by"] is None, snap)


# ═════════════════════════════════════════════════════════════════════════
section("B — restart recovery (generation phase): a dead worker's expired lease is reclaimed, 1 AI call total")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear()
mkuser("userB")
cid = create_cycle("userB")
set_cycle_fields(cid, claimed_by="dead-worker-1",
                  claimed_until=datetime.datetime.utcnow() - datetime.timedelta(minutes=10))

result = run_generation(cid, "worker-2", NOW)
check("reclaimed and generated despite the dead worker's stale claim", result == "push_pending", result)
check("exactly one AI call (not duplicated by the dead worker's claim)", len(gen_calls) == 1, len(gen_calls))

# A STILL-LIVE lease (not expired) must NOT be reclaimed.
gen_calls.clear()
mkuser("userB2")
cid2 = create_cycle("userB2")
# _try_claim's lease-liveness check always uses a FRESH datetime.utcnow()
# read (a deliberate fix — see section M) — so a "genuinely still live"
# lease in this test must be anchored to REAL current time, not the
# test's synthetic NOW (which is just "today at 09:00" and may already be
# in the real past whenever this suite actually runs).
set_cycle_fields(cid2, claimed_by="live-worker",
                  claimed_until=datetime.datetime.utcnow() + datetime.timedelta(minutes=3))

result_live = run_generation(cid2, "worker-2", NOW)
check("a genuinely live claim is NOT stolen by another worker",
      result_live == "skipped_not_claimable", result_live)
check("no AI call made against a live claim", len(gen_calls) == 0, len(gen_calls))


# ═════════════════════════════════════════════════════════════════════════
section("C — restart recovery (push phase): a cycle already push_pending resumes with ZERO new AI calls")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear(); push_calls.clear()
mkuser("userC")
cid = create_cycle("userC")
result = run_generation(cid, "worker-1", NOW)
check("setup: generation succeeded", result == "push_pending", result)
check("setup: exactly one AI call so far", len(gen_calls) == 1, len(gen_calls))

# "Restart": simulate the claim being abandoned mid-push (lease expired, no send happened yet).
set_cycle_fields(cid, claimed_by="dead-worker",
                  claimed_until=datetime.datetime.utcnow() - datetime.timedelta(minutes=1))

push_tickets_to_return["tickets"] = [{"status": "ok"}]
result2 = run_push(cid, "worker-2", NOW)
check("push resumed and completed after the restart", result2 == "completed", result2)
check("STILL exactly one AI call total (push resume never regenerates)", len(gen_calls) == 1, len(gen_calls))
check("exactly one push attempt made", len(push_calls) == 1, len(push_calls))


# ═════════════════════════════════════════════════════════════════════════
section("D — concurrent/duplicate ticks: two overlapping reconcile passes produce exactly ONE AI call")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear()
mkuser("userD")
cid = create_cycle("userD")

# Two "workers" racing the SAME row: worker A claims first (commits,
# releasing the row lock per the established idiom) — worker B's claim
# attempt on the now-leased row must find it unclaimable.
claimed_a = try_claim(cid, "worker-A", ("due", "retryable_failure"), NOW)
check("worker A wins the claim", claimed_a)
claimed_b = try_claim(cid, "worker-B", ("due", "retryable_failure"), NOW)
check("worker B's claim on the same row is refused (already claimed_by A, lease live)", not claimed_b)

# Restore to a clean 'due' state, then prove the real end-to-end concurrency
# property at the reconcile-sweep level: two full reconcile_delivery_cycles
# passes back-to-back (simulating two overlapping 5-minute ticks) against
# the same seeded due cycle.
set_cycle_fields(cid, state="due", claimed_by=None, claimed_until=None)
gen_calls.clear()
dl.reconcile_delivery_cycles(db_factory, NOW, worker_id="tick-1")
dl.reconcile_delivery_cycles(db_factory, NOW, worker_id="tick-2")
check("exactly one AI generation call across BOTH overlapping ticks", len(gen_calls) == 1, len(gen_calls))
check("exactly one canonical DailyBite exists for userD/today", bite_count("userD") == 1, bite_count("userD"))
snap = get_cycle_state("userD")
check("cycle reached push_pending (or further) via exactly one of the two ticks",
      snap["state"] in ("push_pending", "completed"), snap["state"])


# ═════════════════════════════════════════════════════════════════════════
section("E — unread-hold: existing unread bite blocks generation (0 AI calls); clears once read")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear()
mkuser("userE")
yesterday = TODAY - datetime.timedelta(days=1)
db = db_factory()
db.add(DailyBite(id="held-bite-E", user_id="userE", library_item_id="userE-book", title="t", insight="i",
                  reflection="r", action="a", date=yesterday, origin="scheduled", read_at=None))
db.commit()
db.close()

cid = create_cycle("userE")
result = run_generation(cid, "worker-1", NOW)
check("generation correctly held (unread bite blocks it)", result == "held_unread", result)
check("zero AI calls while held", len(gen_calls) == 0, len(gen_calls))

# Now the user reads it — reconciliation should notice and move back to 'due'.
db = db_factory()
b = db.query(DailyBite).filter(DailyBite.id == "held-bite-E").first()
b.read_at = datetime.datetime.utcnow()
db.commit()
db.close()

counts = dl.reconcile_delivery_cycles(db_factory, NOW, worker_id="tick")
snap = get_cycle_state("userE")
# One reconcile pass legitimately cascades a freed hold all the way through
# generation AND push in the SAME tick (held_unread -> due -> push_pending
# -> completed) — it does not stop at 'due' just because that was the
# first transition; the real assertion is that it's no longer held, and
# that generation only happened ONCE.
check("cycle is no longer held after the hold cleared (moved past held_unread)",
      snap["state"] != "held_unread", snap["state"])
check("held_rechecked counter reflects the transition", counts["held_rechecked"] >= 1, counts)
check("generation ran exactly once for the freed hold, in the same tick that freed it",
      counts["generation_processed"] >= 1 and counts["by_result"].get("push_pending", 0) == 1, counts)


# ═════════════════════════════════════════════════════════════════════════
section("F — Expo outcome differentiation: ok / DeviceNotRegistered / transport-unknown / exhausted")
# ═════════════════════════════════════════════════════════════════════════
# F1: ticket 'ok' -> completed (already proven in section A, re-confirmed here explicitly)
mkuser("userF1")
cid = create_cycle("userF1")
run_generation(cid, "w", NOW)
push_tickets_to_return["tickets"] = [{"status": "ok"}]
r = run_push(cid, "w", NOW)
check("F1: ticket 'ok' -> completed", r == "completed", r)

# F2: DeviceNotRegistered -> terminal_failure (dead-letter, no retry)
push_calls.clear()
mkuser("userF2")
cid = create_cycle("userF2")
run_generation(cid, "w", NOW)  # attempts=1 after generation succeeds
attempts_after_gen = get_cycle_state("userF2")["attempts"]
push_tickets_to_return["tickets"] = [{"status": "error", "details": {"error": "DeviceNotRegistered"}}]
r = run_push(cid, "w", NOW)
snap = get_cycle_state("userF2")
check("F2: DeviceNotRegistered -> terminal_failure immediately (no retry burned)",
      r == "terminal_failure" and len(push_calls) == 1, (r, len(push_calls)))
check("F2: attempts advanced by exactly one for the single push attempt "
      "(the counter is shared across generation+push by design, see DeliveryCycle's docstring)",
      snap["attempts"] == attempts_after_gen + 1, (attempts_after_gen, snap["attempts"]))

# F3: transport failure (no tickets returned) -> push_submitted_unknown, retried, NEVER regenerates
gen_calls.clear()
mkuser("userF3")
cid = create_cycle("userF3")
run_generation(cid, "w", NOW)
check("F3 setup: one AI call", len(gen_calls) == 1, len(gen_calls))
push_tickets_to_return["tickets"] = []  # simulates send_push_messages' own transport-failure return
r = run_push(cid, "w", NOW)
check("F3: transport failure -> push_submitted_unknown (not a failure needing regen)",
      r == "push_submitted_unknown", r)
check("F3: NO regeneration happened just because the push outcome was uncertain",
      len(gen_calls) == 1, len(gen_calls))

# F3b: retrying the SAME cycle again while still failing eventually exhausts to
# terminal_failure, and at NO point does generation get called again.
for i in range(dl.MAX_ATTEMPTS + 1):
    snap = get_cycle_state("userF3")
    if snap["state"] in ("terminal_failure", "completed"):
        break
    run_push(cid, "w", NOW)
snap_final = get_cycle_state("userF3")
check("F3b: bounded retries eventually exhaust to terminal_failure",
      snap_final["state"] == "terminal_failure", snap_final["state"])
check("F3b: attempts is bounded at/near MAX_ATTEMPTS, not unbounded",
      snap_final["attempts"] <= dl.MAX_ATTEMPTS, snap_final["attempts"])
check("F3b: STILL exactly one AI call across the entire retry sequence",
      len(gen_calls) == 1, len(gen_calls))
push_tickets_to_return["tickets"] = [{"status": "ok"}]  # reset for later sections


# ═════════════════════════════════════════════════════════════════════════
section("G — bounded catch-up: a cycle past the window expires without ever calling the LLM")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear()
mkuser("userG")
long_ago = NOW - dl.GENERATION_CATCHUP_WINDOW - datetime.timedelta(hours=1)
cid = create_cycle("userG", due_at=long_ago)
result = run_generation(cid, "w", NOW)
check("cycle past the catch-up window expires instead of generating", result == "window_expired", result)
check("zero AI calls for an expired-window cycle", len(gen_calls) == 0, len(gen_calls))


# ═════════════════════════════════════════════════════════════════════════
section("H — notification disabled / no live token: push_pending completes without a send attempt")
# ═════════════════════════════════════════════════════════════════════════
push_calls.clear()
mkuser("userH", token=False)  # no push token at all — e.g. disabled/signed out
cid = create_cycle("userH")
run_generation(cid, "w", NOW)
r = run_push(cid, "w", NOW)
check("no live token -> completed, not a failure", r == "completed", r)
check("no push attempt was made at all", len(push_calls) == 0, len(push_calls))


# ═════════════════════════════════════════════════════════════════════════
section("I — no eligible sources (deleted/locked): terminal_failure, zero AI calls, no stale content")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear()
mkuser("userI", active_book=False)  # no active/processed source at all
cid = create_cycle("userI")
result = run_generation(cid, "w", NOW)
check("no eligible sources -> terminal_failure immediately", result == "terminal_failure", result)
check("zero AI calls when there is nothing eligible to generate from", len(gen_calls) == 0, len(gen_calls))
snap = get_cycle_state("userI")
check("last_error records why, for operator visibility", snap["last_error"] == "no_eligible_sources", snap["last_error"])


# ═════════════════════════════════════════════════════════════════════════
section("J — DeliveryCycle.user_id is a real ON DELETE CASCADE FK (matches ChatTurn's shipped pattern)")
# ═════════════════════════════════════════════════════════════════════════
fk = [c for c in DeliveryCycle.__table__.columns["user_id"].foreign_keys][0]
check("user_id FK targets users.id", str(fk.target_fullname) == "users.id", fk.target_fullname)
check("user_id FK is ondelete=CASCADE", fk.ondelete == "CASCADE", fk.ondelete)


# ═════════════════════════════════════════════════════════════════════════
section("K — streak-alert bounded catch-up + idempotency")
# ═════════════════════════════════════════════════════════════════════════
mkuser("userK", active_book=True, token=True)
db = db_factory()
tok = db.query(PushToken).filter(PushToken.user_id == "userK").first()
tok.notification_hour, tok.notification_minute = 10, 5  # delivery at 10:05 UTC -> T-65 alert slot = 09:00
tok.streak_alerts_enabled = True
db.add(Streak(id="streakK", user_id="userK", current_streak=3, longest_streak=3))
db.add(DailyBite(id="held-K", user_id="userK", library_item_id="userK-book", title="t", insight="i",
                  reflection="r", action="a", date=TODAY - datetime.timedelta(days=1),
                  origin="scheduled", read_at=None, headline="Almost there."))
db.commit()
db.close()

push_calls.clear()
push_tickets_to_return["tickets"] = [{"status": "ok"}]
exact_slot_now = datetime.datetime.combine(TODAY, datetime.time(9, 0))
asyncio.run(notif_service._notify_streak_alert_slot(db_factory, exact_slot_now))
check("K1: exact-slot match fires the streak alert", len(push_calls) == 1, len(push_calls))

# Re-running the SAME exact tick again must NOT double-send (idempotency).
push_calls.clear()
asyncio.run(notif_service._notify_streak_alert_slot(db_factory, exact_slot_now))
check("K2: re-running the same tick does not double-send", len(push_calls) == 0, len(push_calls))

# A DIFFERENT user, missed at the exact tick, still catches up within the window.
mkuser("userK2", active_book=True, token=True)
db = db_factory()
tok2 = db.query(PushToken).filter(PushToken.user_id == "userK2").first()
tok2.notification_hour, tok2.notification_minute = 10, 5
tok2.streak_alerts_enabled = True
db.add(Streak(id="streakK2", user_id="userK2", current_streak=1, longest_streak=1))
db.add(DailyBite(id="held-K2", user_id="userK2", library_item_id="userK2-book", title="t", insight="i",
                  reflection="r", action="a", date=TODAY - datetime.timedelta(days=1),
                  origin="scheduled", read_at=None, headline="Keep going."))
db.commit()
db.close()

push_calls.clear()
late_but_in_window = exact_slot_now + datetime.timedelta(minutes=10)  # within the 20-min catch-up window
asyncio.run(notif_service._notify_streak_alert_slot(db_factory, late_but_in_window))
check("K3: a tick 10 minutes late (within the catch-up window) still catches up", len(push_calls) == 1, len(push_calls))

# A THIRD user, missed well past the window, must NEVER get a stale alert.
mkuser("userK3", active_book=True, token=True)
db = db_factory()
tok3 = db.query(PushToken).filter(PushToken.user_id == "userK3").first()
tok3.notification_hour, tok3.notification_minute = 10, 5
tok3.streak_alerts_enabled = True
db.add(Streak(id="streakK3", user_id="userK3", current_streak=1, longest_streak=1))
db.add(DailyBite(id="held-K3", user_id="userK3", library_item_id="userK3-book", title="t", insight="i",
                  reflection="r", action="a", date=TODAY - datetime.timedelta(days=1),
                  origin="scheduled", read_at=None, headline="Don't lose it."))
db.commit()
db.close()

push_calls.clear()
way_too_late = exact_slot_now + dl.STREAK_ALERT_CATCHUP_WINDOW + datetime.timedelta(minutes=5)
asyncio.run(notif_service._notify_streak_alert_slot(db_factory, way_too_late))
check("K4: a tick past the catch-up window NEVER sends a stale streak alert", len(push_calls) == 0, len(push_calls))


# ═════════════════════════════════════════════════════════════════════════
section("L — regression: window_expired ACTUALLY PERSISTS for a never-claimed row")
# ═════════════════════════════════════════════════════════════════════════
gen_calls.clear()
mkuser("userL")
long_ago = NOW - dl.GENERATION_CATCHUP_WINDOW - datetime.timedelta(hours=1)
cid = create_cycle("userL", due_at=long_ago)
result = run_generation(cid, "w", NOW)
check("L1: phase returns window_expired", result == "window_expired", result)
snap = get_cycle_state("userL")
check("L1: the DB row's state ACTUALLY changed to window_expired (not silently left 'due')",
      snap["state"] == "window_expired", snap["state"])

# A second tick against the now-correctly-terminal row must be a clean no-op
# (not re-select it forever, which is exactly what the pre-fix bug did).
result2 = run_generation(cid, "w2", NOW)
snap2 = get_cycle_state("userL")
check("L2: a second tick against an already-window_expired row does not reprocess it",
      result2 == "skipped_not_claimable", result2)
check("L2: state remains window_expired (terminal, not re-flapped)",
      snap2["state"] == "window_expired", snap2["state"])
check("L: zero AI calls throughout (never should have generated anything this stale)",
      len(gen_calls) == 0, len(gen_calls))

# Same regression, push phase.
mkuser("userL2")
cid2 = create_cycle("userL2")
run_generation(cid2, "w", NOW)
set_cycle_fields(cid2, due_at=long_ago)  # simulate the push phase's own window having elapsed
r = run_push(cid2, "w", NOW)
snap = get_cycle_state("userL2")
check("L3: push-phase window_expired also actually persists",
      r == "window_expired" and snap["state"] == "window_expired", (r, snap["state"]))


# ═════════════════════════════════════════════════════════════════════════
section("M — regression: the real scheduler's worker id is unique per process, not a shared literal")
# ═════════════════════════════════════════════════════════════════════════
check("M1: notification_service exposes a computed _WORKER_ID",
      hasattr(notif_service, "_WORKER_ID"), dir(notif_service))
wid = getattr(notif_service, "_WORKER_ID", "")
check("M2: it is NOT the old hardcoded literal 'scheduler' "
      "(that value made the claim lease's cross-process protection a no-op)",
      wid != "scheduler", wid)
check("M3: it embeds this process's actual pid, so two different OS processes "
      "can never compute the same value",
      str(os.getpid()) in wid, wid)

# Direct proof of the actual vulnerability the audit found: with the SAME
# worker_id, a second "process" CAN silently steal a live claim (the bug);
# with two REAL distinct worker ids (what production now actually uses via
# _WORKER_ID), it correctly cannot.
mkuser("userM")
cid = create_cycle("userM")
claimed_first = try_claim(cid, "same-id", ("due",), NOW)
check("M4 setup: first claim succeeds", claimed_first)
claimed_same_id_again = try_claim(cid, "same-id", ("due",), NOW)
check("M4: demonstrates the OLD bug class — a SHARED worker_id can 're-claim' "
      "its own still-live lease (harmless when it's truly the same worker, but "
      "indistinguishable from a second process if the id were shared in production)",
      claimed_same_id_again)  # same id = "re-claiming your own row", not a security proof either way
set_cycle_fields(cid, state="due", claimed_by=None, claimed_until=None)
claimed_A = try_claim(cid, "real-worker-A", ("due",), NOW)
claimed_B = try_claim(cid, "real-worker-B", ("due",), NOW)
check("M5: with two GENUINELY DISTINCT worker ids (what _WORKER_ID guarantees "
      "across processes), only the first claim succeeds", claimed_A and not claimed_B,
      (claimed_A, claimed_B))


# ═════════════════════════════════════════════════════════════════════════
section("N — regression: streak alert fires correctly for a 00:00-00:40 UTC delivery time")
# ═════════════════════════════════════════════════════════════════════════
# Delivery at 00:20 UTC -> T-65 alert slot = 23:15 UTC on the PREVIOUS
# calendar day. The pre-fix code combined the delivery hour/minute onto
# `now`'s OWN date before subtracting 65 minutes, which is off by exactly
# one day for this band — the alert could never fire, on any tick, ever.
mkuser("userN", active_book=True, token=True)
db = db_factory()
tokN = db.query(PushToken).filter(PushToken.user_id == "userN").first()
tokN.notification_hour, tokN.notification_minute = 0, 20  # cached UTC fallback (no local_hour set)
tokN.streak_alerts_enabled = True
db.add(Streak(id="streakN", user_id="userN", current_streak=2, longest_streak=2))
db.add(DailyBite(id="held-N", user_id="userN", library_item_id="userN-book", title="t", insight="i",
                  reflection="r", action="a", date=TODAY - datetime.timedelta(days=1),
                  origin="scheduled", read_at=None, headline="Midnight save."))
db.commit()
db.close()

push_calls.clear()
# 23:15 on "today" is exactly 65 minutes before 00:20 on "tomorrow" —
# the correct T-65 alert moment for a 00:20 UTC delivery.
midnight_boundary_now = datetime.datetime.combine(TODAY, datetime.time(23, 15))
asyncio.run(notif_service._notify_streak_alert_slot(db_factory, midnight_boundary_now))
check("N: streak alert correctly fires for a 00:20 UTC delivery time at its real T-65 moment "
      "(23:15 the day before) — previously silently never fired for this entire band",
      len(push_calls) == 1, len(push_calls))


# ═════════════════════════════════════════════════════════════════════════
section("O — coverage gap closed: reconcile_delivery_cycles handles a genuinely AWARE `now`")
# ═════════════════════════════════════════════════════════════════════════
# An independent audit noted every check above hand-builds a NAIVE `NOW`
# (datetime.combine(...)) and never actually exercises the aware/naive
# boundary `_naive()` exists to guard — production's real caller
# (`_run_delivery_cycle`) constructs `datetime.now(timezone.utc)`, which IS
# aware. This proves the real entry points survive that, not just that the
# helper function's logic looks right on inspection.
gen_calls.clear()
mkuser("userO")
aware_now = datetime.datetime.now(datetime.timezone.utc)
cid = create_cycle("userO", due_at=aware_now.replace(tzinfo=None))
raised = None
try:
    dl.reconcile_delivery_cycles(db_factory, aware_now, worker_id="aware-tick")
except TypeError as e:
    raised = e
check("reconcile_delivery_cycles does not raise TypeError on a real aware `now` "
      "(the exact crash class `_naive()` exists to prevent)", raised is None, raised)
snap = get_cycle_state("userO")
check("the cycle was actually processed (not silently skipped) under an aware `now`",
      snap is not None and snap["state"] in ("push_pending", "completed"), snap)

# Also drive the actual production entry point (_run_delivery_cycle) once,
# fully mocked, to prove the aware `now` it constructs internally survives
# the full path including delivery_lifecycle.reconcile_delivery_cycles.
raised2 = None
try:
    asyncio.run(notif_service._run_delivery_cycle(db_factory))
except TypeError as e:
    raised2 = e
check("the REAL _run_delivery_cycle (which builds its own aware datetime.now(timezone.utc)) "
      "completes without raising", raised2 is None, raised2)


print(f"\n{'='*60}")
if failures:
    print(f"FAILED: {len(failures)} check(s) failed:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
    sys.exit(0)
