"""
Task 20 (Aug 2026) — durable, restart-safe daily nibble generation +
notification delivery, coordinated with Tasks 3, 4, 15, 16 and 19.

## The gap this closes

Traced in full before writing this file (see the Task 20 design report):
today's scheduler (`notification_service._run_delivery_cycle`, a 5-minute
APScheduler cron) is a stateless query-and-act pass with NO durable record
that a user's daily cycle was ever owed. If the process restarts between
"user X is due for generation" and "user X's push was sent," nothing marks
that cycle incomplete — the in-memory loop cursor is gone, and the next
tick's exact-slot match won't recur until the SAME time TOMORROW. A push
that fails or times out is silently dropped the same way; Expo's per-
message ticket (success/failure/unknown) is parsed then thrown away
without ever being recorded.

## The fix: an explicit state machine, one row per (user, calendar day)

`app.models.delivery.DeliveryCycle` — see that file's docstring for the
full state list. `(user_id, cycle_date)` is a real unique constraint, which
is the whole idempotency guarantee: any tick, any worker, any time after a
restart can look for that user's TODAY row and find out exactly what's
already true, rather than re-deriving intent from side effects.

## Deliberately ADDITIVE, not a replacement

`notification_service._prepare_user_nibbles` / `_notify_delivery_slot` (the
existing on-time T-5/T slot-matching passes) are UNCHANGED — they remain
the fast, already-proven happy path for on-time delivery, and this module
does not touch their unread-blocking, fair-rotation, curiosity-hook-reuse,
or copy-building logic; it CALLS the exact same functions
(`_live_unread_query`, `_select_sources_for_today`, `generate_session_for_item`,
`_feature_bite`, `build_notification_copy`, `send_push_messages`) rather
than reimplementing any of them, so none of those already-audited behaviors
can regress. What this module adds is purely the SECOND layer: a durable
ledger row created alongside the on-time pass, plus a reconciliation sweep
(`reconcile_delivery_cycles`, called every tick from
`notification_service._run_delivery_cycle`, same as the existing passes)
that finds and resumes anything the on-time pass missed — whether it was
never attempted (a full-outage miss) or was claimed but never finished (a
mid-cycle restart).

## Bounded catch-up, never open-ended

`due_at` is fixed at cycle-CREATION time (the moment a tick first
recognizes the obligation — never rewritten) and is the anchor every
window is measured from. `GENERATION_CATCHUP_WINDOW` / `PUSH_CATCHUP_WINDOW`
bound how late a cycle may still be completed; past that, it becomes
`window_expired` — a bounded miss, not an alarm, and never "generate/push
something hours later than it means anything." `attempts` bounds retry
COUNT independently of the time window (`MAX_ATTEMPTS`), per requirement
#7's explicit "define bounded retry."

## Claim/lease idiom

Matches the established pattern (`CleanupTask`, `ReconciliationTask`,
`AccountErasure`, `ChatTurn` — see the Task 20 lifecycle trace for exact
citations): `claimed_by`/`claimed_until`, a short lease
(`CLAIM_LEASE_MINUTES`), `.with_for_update()` immediately before mutating,
commit-before-slow-work, and a `claimed_by == worker_id` re-check before
writing a terminal result — so a late/reclaimed worker can never clobber a
newer worker's own result.

## Uncertain push outcomes are never a reason to regenerate

Requirement #7, verbatim: "Do not regenerate a nibble because push
submission is uncertain." `process_push_phase` below NEVER calls back into
generation — a `push_submitted_unknown` cycle is retried at the SEND step
only, up to `MAX_ATTEMPTS`, then becomes a `terminal_failure` dead-letter
(operator-visible via `last_error`, never silently dropped).

## No production calls under this task

Every test exercising the LLM/Expo paths mocks `generate_session_for_item`/
`send_push_messages` — this module makes no paid AI call or real push send
of its own; it orchestrates the EXISTING functions that do, under test
doubles, exactly like the rest of this task's test suite.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
import logging

from sqlalchemy.exc import IntegrityError

from app.models.delivery import DeliveryCycle
from app.models.bite import DailyBite

logger = logging.getLogger(__name__)

# ── Bounded catch-up policy (requirement #4) ────────────────────────────────
# How late a cycle may still be completed, measured from `due_at` (fixed at
# creation, never rewritten). Generous enough to survive a real multi-hour
# Railway outage; bounded so a day-old obligation is never actioned at all
# (that would mean generating/pushing something at a wildly wrong time of
# day for the user, worse than just missing it and starting fresh tomorrow).
GENERATION_CATCHUP_WINDOW = timedelta(hours=6)
PUSH_CATCHUP_WINDOW = timedelta(hours=6)
# Streak alerts are inherently time-sensitive ("ends in 1 hour") — a much
# tighter bound applies so a stale alert is never sent (requirement #4,
# "never send a misleading 'streak ends in one hour' alert after its
# useful window"). Handled by `_notify_streak_alert_slot`'s own reconcile
# hook below, not the DeliveryCycle ledger (streak alerts carry no AI risk
# and no generation to protect — see the module docstring in
# notification_service.py for why they stay a lighter-weight path).
STREAK_ALERT_CATCHUP_WINDOW = timedelta(minutes=20)

# Bounded retry COUNT, independent of the time window (requirement #7).
MAX_ATTEMPTS = 4

# Claim lease — one tick interval plus slack, matching the scheduler's own
# 5-minute cadence and CleanupTask/ChatTurn's established lease lengths.
CLAIM_LEASE_MINUTES = 5

# Bounded batch size per tick (requirement #8) — never let a long outage's
# backlog overload the DB, Expo or the LLM provider in one pass.
RECONCILE_BATCH_SIZE = 25

# States that mean "this cycle is still open — something may need doing."
OPEN_STATES = (
    "due", "held_unread", "push_pending", "push_submitted_unknown", "retryable_failure",
)
# States that mean "this cycle is finished — nothing will ever act on it again."
TERMINAL_STATES = ("completed", "terminal_failure", "window_expired", "superseded")


def _naive(dt: datetime) -> datetime:
    """This codebase stores/compares timestamps as NAIVE UTC everywhere
    (`datetime.utcnow()`, plain `DateTime` columns with no `timezone=True`
    — see NIBBLE_LOCK_HOURS' own comparison in notification_service.py).
    `_run_delivery_cycle`'s `now`, however, is `datetime.now(timezone.utc)`
    — AWARE, because `_effective_utc_slot` needs `.astimezone()` — so every
    entry point in this module that receives that `now` must strip tzinfo
    before comparing it against a DeliveryCycle column, or Python raises
    `TypeError: can't subtract offset-naive and offset-aware datetimes`.
    Idempotent on an already-naive value (tests may pass one directly)."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def ensure_cycle_for_user(db, user_id: str, cycle_date, now, worker_id: str) -> "DeliveryCycle | None":
    """Get-or-create today's DeliveryCycle row for one user. Concurrency-safe
    via the SAME idiom `generate_session_for_item` already uses for
    `uq_daily_bites_user_item_date`: try the insert, and on the unique
    constraint firing (another tick/worker won the race), roll back and
    re-read the winner instead of raising. Returns None only if the row
    exists but is in a state this call should not touch (should not
    happen in practice — the unique constraint means there is always
    exactly one row per (user, cycle_date), so this is just a defensive
    non-None-on-success guarantee for callers).
    """
    existing = (
        db.query(DeliveryCycle)
        .filter(DeliveryCycle.user_id == user_id, DeliveryCycle.cycle_date == cycle_date)
        .first()
    )
    if existing:
        return existing

    cycle = DeliveryCycle(
        user_id=user_id, cycle_date=cycle_date, state="due", due_at=_naive(now),
    )
    db.add(cycle)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        cycle = (
            db.query(DeliveryCycle)
            .filter(DeliveryCycle.user_id == user_id, DeliveryCycle.cycle_date == cycle_date)
            .first()
        )
    return cycle


def _try_claim(db, cycle_id: str, worker_id: str, expected_states: tuple, now=None) -> "DeliveryCycle | None":
    """Row-level claim: re-read under `with_for_update()`, verify the row is
    still in one of `expected_states` AND not currently leased by a live
    claim, then stamp claimed_by/claimed_until and commit — releasing the
    row lock BEFORE the caller does any slow work (LLM call / Expo HTTP
    call), matching CleanupTask/ChatTurn's established idiom. Returns the
    claimed row, or None if it was no longer claimable (already handled by
    someone else, or moved to a different state since the caller last read
    it) — callers must treat None as "skip this one, not an error."

    Lease timing is ALWAYS a fresh wall-clock read (`datetime.utcnow()`),
    deliberately ignoring the `now` a caller may pass for OTHER purposes
    (window-expiry bookkeeping) — an independent audit found that reusing
    one batch-start `now` for every claim in a `RECONCILE_BATCH_SIZE`-row
    sweep let a lease's `claimed_until` be computed from an increasingly
    stale timestamp as the batch ran (up to 25 sequential LLM calls, then
    25 sequential Expo sends), so a row claimed near the end of a slow
    batch could already read as lease-expired the instant it was written —
    letting a concurrent process "reclaim" a row whose real work was still
    in flight. `now` is accepted only for backward-compatible call sites
    that still pass it; it is NOT used for anything below.
    """
    fresh_now = datetime.utcnow()
    row = db.query(DeliveryCycle).filter(DeliveryCycle.id == cycle_id).with_for_update().first()
    if row is None or row.state not in expected_states:
        db.rollback()
        return None
    if row.claimed_until and row.claimed_by and row.claimed_by != worker_id and row.claimed_until > fresh_now:
        db.rollback()
        return None  # genuinely still leased by a live worker
    row.claimed_by = worker_id
    row.claimed_until = fresh_now + timedelta(minutes=CLAIM_LEASE_MINUTES)
    db.commit()
    db.refresh(row)
    return row


def _finish(db, cycle_id: str, worker_id: str, **fields) -> bool:
    """Write a terminal/next-phase result — but ONLY if `worker_id` still
    owns the claim. A late worker whose lease already expired and was
    reclaimed by someone else must never overwrite that someone else's own,
    possibly-newer result (the exact 'stale claim owner' protection this
    codebase's other claim tables already enforce — see CleanupTask's
    `_cleanup_ledger_resolve`/ChatTurn's `_complete_chat_turn`). Returns
    False (a no-op, not an error) if the claim was lost.
    """
    row = db.query(DeliveryCycle).filter(DeliveryCycle.id == cycle_id).with_for_update().first()
    if row is None or row.claimed_by != worker_id:
        db.rollback()
        return False
    for k, v in fields.items():
        setattr(row, k, v)
    row.claimed_by = None
    row.claimed_until = None
    db.commit()
    return True


def _window_expired(cycle, now, window: timedelta) -> bool:
    return (_naive(now) - cycle.due_at) > window


def process_generation_phase(db, cycle: DeliveryCycle, worker_id: str, now) -> str:
    """Advance one 'due' (or expired-lease-reclaimed) cycle through
    generation. Reuses EXISTING, already-audited logic wholesale —
    `_live_unread_query` for the hold check, `reconcile_free_lock_state` +
    the same active/eligible query `_prepare_user_nibbles` uses, `_select_
    sources_for_today` for fair rotation, and `generate_session_for_item`
    itself for the actual generation + its own IntegrityError dedup — this
    function only adds the durable claim/state wrapper around them. Returns
    the resulting state, for the caller's counters/logging.
    """
    from app.models.user import User
    from app.models.library import LibraryItem
    from app.services.entitlement_service import reconcile_free_lock_state
    from app.services.notification_service import (
        _live_unread_query, _select_sources_for_today, _load_growth_state, _read_length_for, _build_profile_dict,
    )
    from app.services.session_service import generate_session_for_item, SessionGenerationError
    from app.config import get_settings

    # Claim BEFORE the window-expiry write, not after — an independent
    # audit found the original order let `_finish` silently no-op for any
    # row that was never claimed (claimed_by is None, which never equals a
    # real worker_id), so a naturally-aged-out row's 'window_expired'
    # transition never actually persisted: `state` stayed 'due' forever,
    # and every later tick re-selected and re-"expired" it identically. A
    # claim failure here (row already claimed live elsewhere, or moved to
    # a different state) is reported the same as any other skip.
    claimed = _try_claim(db, cycle.id, worker_id, ("due", "retryable_failure"), now)
    if claimed is None:
        return "skipped_not_claimable"
    cycle = claimed

    if _window_expired(cycle, now, GENERATION_CATCHUP_WINDOW):
        _finish(db, cycle.id, worker_id, state="window_expired")
        return "window_expired"

    user = db.query(User).filter(User.id == cycle.user_id).first()
    if not user:
        _finish(db, cycle.id, worker_id, state="terminal_failure", last_error="user_not_found")
        return "terminal_failure"

    reconcile_free_lock_state(db, user)
    unread_list = _live_unread_query(db, user)
    if unread_list:
        _finish(db, cycle.id, worker_id, state="held_unread")
        return "held_unread"

    active = (
        db.query(LibraryItem)
        .filter(
            LibraryItem.user_id == user.id, LibraryItem.is_active.is_(True),
            LibraryItem.processed.is_(True), LibraryItem.deletion_state.is_(None),
        )
        .order_by(LibraryItem.id.asc())
        .all()
    )
    if not active:
        _finish(db, cycle.id, worker_id, state="terminal_failure", last_error="no_eligible_sources")
        return "terminal_failure"

    settings = get_settings()
    cap = settings.premium_bites_per_day if user.effective_premium else settings.free_bites_per_day
    selected = _select_sources_for_today(active, min(cap, len(active)), cycle.cycle_date)
    growth_state = _load_growth_state(db, cycle.user_id)

    made_bite = None
    last_error = None
    for item in selected:
        try:
            made_bite = generate_session_for_item(
                db, user=user, item=item,
                read_length=_read_length_for(growth_state, item),
                profile=_build_profile_dict(growth_state, item),
                today=cycle.cycle_date, origin="scheduled",
            )
            break  # one canonical nibble per cycle — requirement #5's "at most one canonical nibble"
        except SessionGenerationError as e:
            last_error = f"generation_failed:{e.message}"[:250]
        except Exception as e:
            last_error = f"generation_error:{type(e).__name__}"[:250]

    if made_bite:
        _finish(db, cycle.id, worker_id, state="push_pending", daily_bite_id=made_bite.id,
                library_item_id=made_bite.library_item_id, attempts=cycle.attempts + 1)
        return "push_pending"

    attempts = cycle.attempts + 1
    if attempts >= MAX_ATTEMPTS:
        _finish(db, cycle.id, worker_id, state="terminal_failure", attempts=attempts,
                last_error=last_error or "generation_exhausted")
        return "terminal_failure"
    _finish(db, cycle.id, worker_id, state="retryable_failure", attempts=attempts,
            last_error=last_error or "generation_failed")
    return "retryable_failure"


# Expo error codes that mean "this token will never work again" — never
# retried, dead-lettered immediately rather than burning MAX_ATTEMPTS on
# something no retry can fix.
_EXPO_TERMINAL_ERRORS = {"DeviceNotRegistered"}


def process_push_phase(db, cycle: DeliveryCycle, worker_id: str, now) -> str:
    """Advance one 'push_pending' (or 'push_submitted_unknown'-retry, or
    reclaimed-'push_claimed') cycle through the actual send. Re-fetches
    LIVE push tokens at send time (not a cached list from cycle-creation
    time), so a signed-out/replaced/disabled token is naturally excluded —
    no separate invalidation hook needed for that lifecycle case (see the
    module docstring's "deliberately additive" note). Captures the Expo
    per-message ticket outcome for the first time in this codebase — the
    existing `_notify_delivery_slot`/`_notify_streak_alert_slot` discard it
    entirely (see the Task 20 lifecycle trace, point 6).
    """
    from app.models.push_token import PushToken
    from app.models.user import User
    from app.services.notification_service import (
        build_notification_copy, _notification_data, send_push_messages,
    )
    from app.config import get_settings
    import asyncio

    # Claim before checking/writing window-expiry — see the identical note
    # in process_generation_phase for why order matters here (a claim-less
    # `_finish` silently no-ops, since `_finish` only ever writes for the
    # worker_id that currently owns the row).
    claimed = _try_claim(db, cycle.id, worker_id, ("push_pending", "push_submitted_unknown"), now)
    if claimed is None:
        return "skipped_not_claimable"
    cycle = claimed

    if _window_expired(cycle, now, PUSH_CATCHUP_WINDOW):
        _finish(db, cycle.id, worker_id, state="window_expired")
        return "window_expired"

    user = db.query(User).filter(User.id == cycle.user_id).first()
    bite = db.query(DailyBite).filter(DailyBite.id == cycle.daily_bite_id).first()
    if not user or not bite:
        _finish(db, cycle.id, worker_id, state="terminal_failure", last_error="user_or_bite_missing")
        return "terminal_failure"

    if bite.read_at is not None:
        # The user already read it via the in-app path (or an earlier,
        # already-delivered push) before this reconciliation pass got to
        # it — nothing owed, not a failure.
        _finish(db, cycle.id, worker_id, state="completed")
        return "completed"

    live_tokens = [
        t.token for t in db.query(PushToken)
        .filter(PushToken.user_id == user.id, PushToken.notifications_enabled.is_(True))
        .all()
    ]
    if not live_tokens:
        # Nothing owed: notifications disabled or every token replaced/
        # signed out since generation — the nibble is already generated
        # and available in-app; that is a completed cycle, not a failure.
        _finish(db, cycle.id, worker_id, state="completed")
        return "completed"

    item_title, item_id = bite.source, bite.library_item_id
    kind = "fresh" if bite.date >= cycle.cycle_date else "forgotten"
    title, body = build_notification_copy(kind, item_title, bite, cycle.cycle_date)
    data = _notification_data(item_id, bite)
    messages = [{"to": tok, "title": title, "body": body, "sound": "default", "data": data} for tok in live_tokens]

    settings = get_settings()
    try:
        tickets = asyncio.run(send_push_messages(messages, getattr(settings, "expo_access_token", "")))
    except RuntimeError:
        # Already inside an event loop (e.g. called from async scheduler
        # context) — run on the loop directly instead of asyncio.run(),
        # which refuses to nest.
        loop = asyncio.get_event_loop()
        tickets = loop.run_until_complete(send_push_messages(messages, getattr(settings, "expo_access_token", "")))

    attempts = cycle.attempts + 1
    if not tickets:
        # Transport-level failure (network error, non-2xx, malformed JSON —
        # send_push_messages already logged it and returned []) — outcome
        # genuinely unknown, NEVER regenerate (requirement #7).
        if attempts >= MAX_ATTEMPTS:
            _finish(db, cycle.id, worker_id, state="terminal_failure", attempts=attempts,
                    last_error="push_outcome_unknown_exhausted")
            return "terminal_failure"
        _finish(db, cycle.id, worker_id, state="push_submitted_unknown", attempts=attempts,
                last_error="push_transport_failed")
        return "push_submitted_unknown"

    statuses = {t.get("status") for t in tickets}
    if "ok" in statuses:
        _finish(db, cycle.id, worker_id, state="completed", attempts=attempts)
        return "completed"

    terminal = any(
        t.get("status") == "error" and t.get("details", {}).get("error") in _EXPO_TERMINAL_ERRORS
        for t in tickets
    )
    if terminal:
        _finish(db, cycle.id, worker_id, state="terminal_failure", attempts=attempts,
                last_error="expo_device_not_registered")
        return "terminal_failure"

    if attempts >= MAX_ATTEMPTS:
        _finish(db, cycle.id, worker_id, state="terminal_failure", attempts=attempts,
                last_error="push_error_exhausted")
        return "terminal_failure"
    _finish(db, cycle.id, worker_id, state="push_submitted_unknown", attempts=attempts,
            last_error="push_error_retryable")
    return "push_submitted_unknown"


def discover_and_create_due_cycles(db, now, worker_id: str) -> int:
    """Catch-up net (requirement #3: 'a scheduler tick must be a wake-up
    mechanism, not the sole record that work existed') — finds any user
    whose T-5 generation slot has ALREADY passed today (not just users
    whose slot equals right-now, which is all the existing on-time pass
    checks) and who has no DeliveryCycle row for today yet, bounded by
    GENERATION_CATCHUP_WINDOW so this can never reach back past what the
    catch-up policy would honor anyway. Covers the case the on-time pass
    structurally cannot: the ENTIRE process being down during a user's
    exact slot, not just an interrupted mid-cycle worker (that case is
    handled by `_try_claim`'s expired-lease reclaim instead, since a row
    already exists for it).
    """
    from app.models.push_token import PushToken
    from app.models.user import User
    from app.services.notification_service import _effective_utc_slot

    # `_effective_utc_slot` needs a genuinely UTC-aware `now` (it calls
    # `.astimezone(tz)`, which on a NAIVE datetime silently assumes the
    # HOST's local time zone as the source — wrong here) — kept as-is for
    # that one call. Everything else in this function compares against
    # DeliveryCycle's naive-UTC columns, so `now_naive` is used there.
    now_naive = _naive(now)
    created = 0
    rows = (
        db.query(PushToken, User)
        .join(User, User.id == PushToken.user_id)
        .filter(PushToken.notifications_enabled.is_(True))
        .all()
    )
    today = now_naive.date()
    seen_users = set()
    for tok, user in rows:
        if user.id in seen_users:
            continue
        eff_hour, eff_minute = _effective_utc_slot(
            tok.notification_local_hour, tok.notification_local_minute,
            user.timezone, tok.notification_hour, tok.notification_minute, now,
        )
        slot_dt = datetime(today.year, today.month, today.day, eff_hour, eff_minute) - timedelta(minutes=5)
        if slot_dt > now_naive:
            continue  # not due yet today
        if (now_naive - slot_dt) > GENERATION_CATCHUP_WINDOW:
            continue  # already outside the catch-up window — no point creating a row just to immediately expire it
        seen_users.add(user.id)
        existing = (
            db.query(DeliveryCycle)
            .filter(DeliveryCycle.user_id == user.id, DeliveryCycle.cycle_date == today)
            .first()
        )
        if existing:
            continue
        cycle = ensure_cycle_for_user(db, user.id, today, slot_dt, worker_id)
        if cycle is not None:
            created += 1
    return created


def reconcile_delivery_cycles(db_factory, now, worker_id: str = "reconciler") -> dict:
    """The main entry point — called every tick from
    `notification_service._run_delivery_cycle`, alongside (not instead of)
    the existing on-time passes. Bounded-batch (requirement #8): processes
    at most RECONCILE_BATCH_SIZE rows per phase per tick, so a long
    backlog drains gradually across several ticks instead of overloading
    the DB/Expo/LLM provider in one pass. Returns a counts dict for
    privacy-safe observability (ids/counts only — never source text,
    hooks, tokens or bite content, matching this codebase's existing
    telemetry discipline).
    """
    counts = {
        "created": 0, "generation_processed": 0, "push_processed": 0,
        "held_rechecked": 0, "by_result": {},
    }

    def _bump(result: str):
        counts["by_result"][result] = counts["by_result"].get(result, 0) + 1

    # `discover_and_create_due_cycles` needs the ORIGINAL (aware) `now` for
    # `_effective_utc_slot`; every SQL filter/comparison below it needs the
    # naive form to match DeliveryCycle's naive-UTC columns — see `_naive`'s
    # docstring for why mixing them raises TypeError, not just gives a
    # wrong answer.
    now_naive = _naive(now)

    with db_factory() as db:
        counts["created"] = discover_and_create_due_cycles(db, now, worker_id)

        # Re-check held rows: a hold that's cleared (user read the earlier
        # bite) moves back to 'due' for the generation step to pick up.
        from app.models.user import User
        from app.services.notification_service import _live_unread_query

        held = (
            db.query(DeliveryCycle)
            .filter(DeliveryCycle.state == "held_unread", DeliveryCycle.cycle_date == now_naive.date())
            .limit(RECONCILE_BATCH_SIZE)
            .all()
        )
        for cycle in held:
            if _window_expired(cycle, now_naive, GENERATION_CATCHUP_WINDOW):
                # Claim before writing — see the identical note in
                # process_generation_phase/process_push_phase; a claim-less
                # `_finish` silently no-ops. A failed claim here just means
                # some other worker is already handling this row; skip it,
                # the same as any other unclaimable row.
                if _try_claim(db, cycle.id, worker_id, ("held_unread",), now_naive) is not None:
                    _finish(db, cycle.id, worker_id, state="window_expired")
                    counts["held_rechecked"] += 1
                continue
            user = db.query(User).filter(User.id == cycle.user_id).first()
            if not user:
                continue
            if not _live_unread_query(db, user):
                claimed = _try_claim(db, cycle.id, worker_id, ("held_unread",), now_naive)
                if claimed:
                    _finish(db, cycle.id, worker_id, state="due")
                    counts["held_rechecked"] += 1

        due_rows = (
            db.query(DeliveryCycle)
            .filter(DeliveryCycle.cycle_date == now_naive.date(),
                    DeliveryCycle.state.in_(("due", "retryable_failure")))
            .filter((DeliveryCycle.claimed_until.is_(None)) | (DeliveryCycle.claimed_until < now_naive))
            .limit(RECONCILE_BATCH_SIZE)
            .all()
        )
        for cycle in due_rows:
            result = process_generation_phase(db, cycle, worker_id, now_naive)
            counts["generation_processed"] += 1
            _bump(result)

        push_rows = (
            db.query(DeliveryCycle)
            .filter(DeliveryCycle.cycle_date == now_naive.date(),
                    DeliveryCycle.state.in_(("push_pending", "push_submitted_unknown")))
            .filter((DeliveryCycle.claimed_until.is_(None)) | (DeliveryCycle.claimed_until < now_naive))
            .limit(RECONCILE_BATCH_SIZE)
            .all()
        )
        for cycle in push_rows:
            result = process_push_phase(db, cycle, worker_id, now_naive)
            counts["push_processed"] += 1
            _bump(result)

    logger.info(
        "delivery_cycle reconcile: created=%d generation=%d push=%d held_rechecked=%d results=%s",
        counts["created"], counts["generation_processed"], counts["push_processed"],
        counts["held_rechecked"], counts["by_result"],
    )
    return counts
