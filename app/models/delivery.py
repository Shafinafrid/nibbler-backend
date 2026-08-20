"""
Task 20 (Aug 2026) — durable delivery-cycle ledger, the record that makes
daily nibble generation + notification delivery survive a Railway restart,
deployment, crash or long stall without losing, duplicating or misdating a
user's day.

Before this table, "did user X's daily cycle happen today" was never a
question the database could answer — it was reconstructed each tick from
side effects (does a DailyBite row for today exist? has ~24h passed since
the last one?) with no record of intent. A process restart between "this
user is due" and "this user's push was sent" left nothing to resume: the
partial cycle was silently lost until the exact same 5-minute slot recurred
the FOLLOWING day. See `app/services/delivery_lifecycle.py`'s module
docstring for the full state machine and reconciliation design; this file
is deliberately just the schema + the invariants it enforces.

`(user_id, cycle_date)` is the whole idempotency anchor — enforced as a real
unique constraint, not just application discipline — so `INSERT`-then-
`IntegrityError`-then-re-read-the-winner (this codebase's established
concurrent-insert idiom — see ChatTurn, generate_session_for_item) is what
protects "two ticks/workers both think this user is due right now" from
ever producing two cycles for the same day.
"""
from sqlalchemy import (
    Column, String, Integer, Date, DateTime, ForeignKey, UniqueConstraint, Index, func,
)
from app.database import Base
from app.models.user_data import _uuid


class DeliveryCycle(Base):
    """One row per (user, calendar day) covering BOTH generation and push —
    not two separate ledgers — because they are sequential phases of one
    logical daily obligation, and a single row lets one reconciliation pass
    resume either phase without a join.

    `state` (see delivery_lifecycle.py for the full transition table). A
    worker actively holding the claim lease does NOT get its own separate
    state value — exactly like ChatTurn's 'pending' covers both "never
    claimed" and "claimed, in progress," distinguished by `claimed_by`/
    `claimed_until` alone, not a state string. So 'due' means "either
    nothing has been attempted, or a worker currently holds the generation
    lease" (check `claimed_until` to tell which), and 'push_pending'/
    'push_submitted_unknown' mean the same for the push lease:
      'due'                  — owed; not yet generated (see claimed_until
                                above for "currently being worked").
      'held_unread'          — correctly skipped: an earlier unread bite
                                still blocks generation (not a failure;
                                re-checked every tick, same as the pre-
                                existing hold-until-read rule).
      'push_pending'         — DailyBite exists (`daily_bite_id` set), push
                                not yet attempted (or currently being sent).
      'push_submitted_unknown' — Expo was called but the outcome is not
                                confirmed (timeout/exception/malformed
                                response) — retried, NEVER regenerated.
      'completed'            — terminal success: either a push ticket came
                                back 'ok', or there was legitimately nothing
                                to push (notifications disabled / no live
                                token) with the nibble already generated
                                and available in-app.
      'retryable_failure'    — a transient error at either phase; eligible
                                for another attempt next tick, bounded by
                                `attempts` and the catch-up window.
      'terminal_failure'     — bounded retries exhausted, or a definitive
                                non-retryable error (no eligible source,
                                Expo DeviceNotRegistered) — operator-visible
                                dead-letter, never retried again.
      'window_expired'       — the bounded catch-up window elapsed before
                                this cycle could complete — a bounded miss,
                                not an alarm; there is always a fresh 'due'
                                row for the NEXT calendar day.
      'superseded'           — invalidated by an account/source lifecycle
                                event before completion (see
                                delivery_lifecycle.py's invalidation notes).

    `claimed_by`/`claimed_until` are reused across BOTH the generation-claim
    and push-claim phases (a cycle is only ever in one claimed phase at a
    time, so one lease pair is enough — matching this codebase's other
    single-phase claim tables rather than inventing a second pair).
    `attempts` is likewise one bounded counter shared by both phases: it
    exists to cap total retries for the cycle, not to distinguish which
    phase failed (that's what `state`/`last_error` already record).
    """
    __tablename__ = "delivery_cycles"
    __table_args__ = (
        UniqueConstraint("user_id", "cycle_date", name="uq_delivery_cycle_user_date"),
        # reconcile_delivery_cycles' three hot queries all filter
        # `cycle_date == today AND state IN (...)` with no user_id
        # predicate — an independent audit noted neither gets the
        # leftmost-prefix benefit of the (user_id, cycle_date) unique
        # constraint's index, and this table has no retention/archival job
        # (one row per user per calendar day, forever), so this composite
        # index is what keeps those queries an index scan rather than a
        # full-table scan as history accumulates.
        Index("ix_delivery_cycles_cycle_date_state", "cycle_date", "state"),
    )

    id = Column(String, primary_key=True, default=_uuid)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    cycle_date = Column(Date, nullable=False)  # matches daily_bites.date's UTC-day convention
    state = Column(String, nullable=False, default="due", index=True)
    library_item_id = Column(String, nullable=True)  # no FK — matches daily_bites.library_item_id (a deleted book must not block/crash this row)
    daily_bite_id = Column(String, nullable=True)     # no FK, same reason; set once generated
    claimed_by = Column(String, nullable=True)
    claimed_until = Column(DateTime, nullable=True)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String, nullable=True)  # safe, short, operator-facing only — never source text/hooks
    due_at = Column(DateTime, nullable=False)   # the ORIGINAL computed due time (UTC) — the catch-up window's fixed anchor, never rewritten
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
