"""
Free-tier lifetime source entitlement + downgrade lock selection (Task 2,
Aug 2026 pre-publication audit; remediated Aug 2026 after Hermes's FIRST
audit; remediated AGAIN after Hermes's SECOND audit — see the Task 2
remediation completion reports for the full lists of findings each pass
closes).

Free accounts get THREE PERMANENT successful-source entitlements — a
lifetime count, immune to deletion, not a live row count. When Premium/trial/
complimentary access ends, exactly three of the account's successfully
processed sources stay unlocked for Free use (the user's own prior pick if
one exists, else the three most recently active); every other source stays
stored and visible but locked: no new sessions, no Review, no Connect/chat,
no active-source rotation, no scheduled generation, no further paid AI work.

Locking is a FUNCTION of current entitlement state and a persisted
selection — never a boolean mass-flipped on every purchase/expiry event
across every row. `LibraryItem.is_unlocked_selection` is the persisted ≤3-
item selection; whether it actually matters is decided fresh, on read, by
`is_source_unlocked()`.

Every route that gates on lock state must call `is_source_unlocked()` (or,
indirectly, rely on `reconcile_free_lock_state()` having already run via the
`get_current_user` dependency) rather than re-deriving its own check.

── GLOBAL POSTGRESQL LOCK ORDER (2nd-audit remediation) ─────────────────────
Every function in this module (and `delete_library_item` in
app/routers/library.py, which performs the same class of accounting write)
that needs to lock BOTH a `users` row and one or more `library_items` rows in
the same transaction acquires them in exactly this order, with no exception:

    1. the USER row (`SELECT ... FOR UPDATE` on `users`)
    2. the LIBRARY_ITEM row(s), in ASCENDING `id` order when more than one

`renew_reservation_lease` is the one function that touches ONLY a
`library_items` row (no `users` write) — it never acquires a user lock at
all, which is safe: a transaction that holds only one resource can never be
part of a two-resource deadlock cycle.

This single rule is what closes the credible deadlock Hermes's second audit
found: the original code had `reserve_free_capacity` locking item-then-user,
while `_reap_stale_reservations` (called from inside `reserve_free_capacity`,
already holding the user lock) updated OTHER item rows, and
`release_reservation`/`finalize_successful_processing` also locked
item-then-user — two DIFFERENT orders touching the same two resource types is
exactly the shape of a deadlock cycle. Forcing every one of them onto
user-then-item makes a circular wait-for graph between these resources
provably impossible (a standard total-lock-ordering deadlock-prevention
argument) — proven against real Postgres by constructing the original cycle
under the NEW code and confirming neither transaction ever blocks on the
other; see the Task 2 remediation completion report.

── Reservation model with a RENEWABLE lease (2nd-audit remediation #3) ──────
Consuming one of the three lifetime slots is a TWO-PHASE operation:

  1. RESERVE, before any paid embedding/indexing/OCR call begins
     (`reserve_free_capacity`) — atomically, under the global lock order, so
     two concurrent uploads at 2/3 can never both proceed to pay for
     processing when only one of them can ever be kept. A reservation mints a
     `reservation_lease_token` and a `reservation_lease_expires_at`.
  2. CONVERT the reservation into permanent consumption on the item's first
     successful completion (`finalize_successful_processing`), or RELEASE it
     back (`release_reservation`) if processing fails.

A reservation's lease is a LIVE, RENEWABLE deadline, not a fixed 30-minute
clock that started ticking once: a caller doing real, checkpointed work (the
per-page progress callback during OCR) calls `renew_reservation_lease` on
every checkpoint, extending the deadline from THAT moment — so a genuinely
active multi-hour OCR job on a huge scanned book is never reaped out from
under itself just because more than 30 minutes have passed in total. Only a
lease nothing has renewed past its current deadline is reapable. Every
reservation/renewal/finalization call that carries a `lease_token` is
verified against the item's CURRENT stored token — a worker whose token has
been superseded (the item was reaped and a NEW attempt reserved a NEW token)
gets told no and must stop, which is what stops a stale retry from stealing
or double-converting an attempt a fresher worker already owns.

This is what makes "no rejected/abandoned/superseded item may leave vectors
or other external artifacts behind" true: capacity is denied (or an attempt
is invalidated) BEFORE — or immediately upon detecting — an over-commitment,
and every caller that reaches a terminal "this attempt does not count"
outcome is expected to run compensating cleanup (see
app/routers/library.py's `_release_reservation_after_failure`, which pairs
every `release_reservation` call with an idempotent, ownership-scoped
Pinecone vector delete).

── Downgrade accounting (2nd-audit remediation #1) ──────────────────────────
Hermes's second audit reproduced: a Premium/trial user creates 3+ sources
(each reserved as 'premium' — exempt, uncapped, never touching the lifetime
counter by design). On downgrade, `finalize_lock_selection` retained exactly
3 of them (correct) but never promoted them into PERMANENT consumption — so
`successful_sources_total` stayed 0 and the account could then also upload 3
MORE "free" sources, i.e. 6 total lifetime slots instead of 3.

The fix: retaining a source at downgrade whose `entitlement_status` is not
already 'consumed' (i.e. it was 'premium', or an unattributed pre-cutover
'grandfathered' row) is EXACTLY what "occupies one of the three permanent
Free entitlements" means — so `finalize_lock_selection` now promotes each
such retained item to 'consumed' and increments `successful_sources_total`,
capped so the total can never exceed `free_source_limit()`. When capacity is
already exhausted (e.g. the account consumed 3 Free slots BEFORE ever going
Premium), no new promotion happens; the retained set instead prefers
whichever eligible sources are ALREADY 'consumed' (costing no new capacity)
over ones that would need it — never creating extra capacity, and never
letting the retained set exceed what the account can actually afford. See
`finalize_lock_selection` below for the full algorithm and the completion
report for the exact PostgreSQL proof of the previously-reproduced failure
now fixed.
"""
import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.library import LibraryItem
from app.models.user import User, TRIAL_DAYS

logger = logging.getLogger(__name__)
settings = get_settings()

# Default lease duration for a 'pending' reservation, both on first
# reservation and on every renewal — generous relative to the documented
# background-ingestion budget (~5 minutes per nibbler-backend/CLAUDE.md) so a
# slow-but-HEALTHY, unrenewed job is never reaped out from under itself. Real
# multi-checkpoint work (OCR) renews well before this elapses on each page;
# see app/routers/library.py's `_run_ocr`.
RESERVATION_TTL_MINUTES = 30

_ACTIVE_RESERVATION_STATES = ("pending", "consumed", "premium")


def free_source_limit() -> int:
    """The permanent lifetime count of successful sources a Free account may
    keep unlocked. Reuses the existing `free_upload_limit` setting (3)."""
    return settings.free_upload_limit


# ── Canonical entitlement resolver (Task 8, Aug 2026) ───────────────────────
# The ONE place that turns is_premium/premium_until/entitlement_source/
# trial_anchor_at into a human-facing result. `GET /entitlement` returns this
# directly; `effective_premium`/`is_source_unlocked` remain the enforcement
# gates (unchanged) — this function never gates anything itself, it only
# REPORTS what the gates already decided, plus provenance the boolean gates
# don't carry (why access exists, since when, whether it was ever paid).

def resolve_entitlement(user: User) -> dict:
    """Returns the canonical entitlement result — access, source, dates,
    whether paid access has ever been held, and last-sync state. Every
    consumer (backend response, mobile display) must use this instead of
    re-deriving its own reading of is_premium/premium_until."""
    now = datetime.utcnow()
    access = "premium" if user.effective_premium else "free"

    if user.effective_premium:
        if user.entitlement_source == "complimentary":
            source = "complimentary"
        elif user.entitlement_source == "paid":
            source = "paid"
        elif user.premium_until:
            # entitlement_source is unset but premium_until IS — only the
            # paid/webhook write paths ever set premium_until, so this is a
            # pre-Task-8 real-subscriber row the one-time backfill (see
            # database.py) doesn't touch (that backfill only targets
            # is_premium=True rows). Correctly 'paid', not a guess.
            source = "paid"
        elif user.is_premium:
            # Audit finding (Aug 2026): bare is_premium=True with NEITHER
            # entitlement_source NOR premium_until set is exactly the shape
            # of a PRE-Task-8 manual comp — is_premium had no writer at all
            # before this task, so every existing True row was set by hand,
            # which is precisely what "reserved for comps" (the pre-
            # existing convention _plan_label in routers/auth.py already
            # encodes) means. The one-time backfill in database.py should
            # already have set entitlement_source='complimentary' for these
            # on boot; this branch is the safety net for whatever it
            # doesn't yet cover. Reporting 'paid' here (an earlier version
            # of this function did) would mislabel exactly the accounts
            # this whole feature exists to serve — e.g. the founder's own.
            source = "complimentary"
        else:
            source = "trial"
    else:
        source = "free"

    # Start/expiry are only meaningful for a currently-active grant — a
    # lapsed premium_until or a cleared trial has no "current" window.
    starts_at = None
    expires_at = None
    if source == "trial":
        anchor = user.trial_anchor_at or user.created_at
        starts_at = anchor
        expires_at = (anchor + timedelta(days=TRIAL_DAYS)) if anchor else None
    elif source in ("paid", "complimentary"):
        expires_at = user.premium_until if (user.premium_until and user.premium_until > now) else None
        # is_premium with no premium_until is a LIFETIME grant (e.g. the
        # founder's own account) — genuinely no expiry, not "unknown".

    return {
        "access": access,                                   # 'free' | 'premium'
        "source": source,                                    # 'free' | 'trial' | 'paid' | 'complimentary'
        "starts_at": starts_at.isoformat() if starts_at else None,
        "expires_at": expires_at.isoformat() if expires_at else None,
        "is_lifetime": source in ("paid", "complimentary") and user.is_premium and not user.premium_until,
        "has_held_paid_entitlement": bool(user.has_held_paid_entitlement),
        "last_synced_at": user.premium_synced_at.isoformat() if user.premium_synced_at else None,
    }


# ── Effective access (the one function every enforcement point must use) ────

def is_source_unlocked(user: User, item: LibraryItem) -> bool:
    """The single authoritative answer. Premium/trial/complimentary access
    (`effective_premium`) unlocks every owned source; otherwise only the
    persisted ≤3-item selection is usable. Never reimplement this check
    inline — a second copy is exactly how a route silently disagrees with
    the rest of the app.

    A fenced item (Task 2 lifecycle remediation, Follow-up 2A —
    `entitlement_status == 'released'` on a `processed=True` row is the
    mixed-version-cutover fencing signature; see
    `reconcile_unaccounted_processed_items` and the database trigger in
    app/database.py) is excluded UNCONDITIONALLY, before the Premium
    bypass — otherwise a Premium account's own fenced row would read as
    accessible just because Premium skips the selection check entirely,
    even though its lifetime accounting hasn't happened yet.

    Task 2 closeout (Verified Blocker 6): a TOMBSTONED item
    (`deletion_state IS NOT NULL`) is excluded the same unconditional
    way, for the same reason — this is the single authoritative
    "is this source usable" answer, so a Premium account's own
    already-deleted-but-not-yet-hard-deleted row must never read as
    accessible just because Premium skips every other check."""
    if item.deletion_state is not None:
        return False
    if item.processed and item.entitlement_status == "released":
        return False
    if user.effective_premium:
        return True
    return bool(item.is_unlocked_selection)


# ── Lifetime successful-source counter (fast pre-check only) ────────────────

def can_accept_new_upload(user: User) -> bool:
    """Cheap, request-time preview of whether this account has room for one
    more source. NOT the authoritative gate — the real, concurrency-safe
    decision happens in reserve_free_capacity."""
    if user.effective_premium:
        return True
    return user.successful_sources_total < free_source_limit()


def _free_limit_message() -> str:
    return (
        f"Free includes {free_source_limit()} permanent sources. Upgrade to "
        "Premium for unlimited uploads — deleting a source doesn't free up a new one."
    )


# ── Reservation primitive — acquire BEFORE any paid work ────────────────────

def _reap_stale_reservations(db: Session, user: User) -> int:
    """Reclaim this account's own abandoned 'pending' reservations — a
    lease nothing has renewed past its current deadline. Only ever called
    while the caller already holds `user`'s row lock (global lock order:
    user first), so it can't race a concurrent reservation attempt for the
    same account. Locks each stale item row (ascending id — the second half
    of the global order) before mutating it, and does not commit — the
    caller is about to write more to the same transaction anyway.

    Deliberately does NOT clear `reservation_lease_token` on the reaped row:
    an old worker still holding that token will find every subsequent
    `renew_reservation_lease`/`finalize_successful_processing` call fails,
    because a FRESH reservation (if the item is retried) mints a brand-new
    token that no longer matches what the old worker is holding — that
    mismatch, not a cleared token, is what stops a stale worker from
    double-converting a reservation a newer attempt now owns.

    Returns the number of reservations actually reaped (0 if none) — used
    by `reap_all_stale_reservations`'s scheduled sweep (Task 15 remediation)
    to report real work done, matching every other autonomous pass in this
    module's own never-collapse-to-one-number contract.
    """
    now = datetime.utcnow()
    legacy_cutoff = now - timedelta(minutes=RESERVATION_TTL_MINUTES)
    stale_ids = [
        pid for (pid,) in db.query(LibraryItem.id)
        .filter(
            LibraryItem.user_id == user.id,
            LibraryItem.entitlement_status == "pending",
        )
        .filter(
            (
                (LibraryItem.reservation_lease_expires_at.isnot(None))
                & (LibraryItem.reservation_lease_expires_at < now)
            )
            # Defensive fallback for a 'pending' row with no lease expiry at
            # all (a pre-lease-remediation row that predates this column) —
            # fall back to the original reserved_at cutoff so it can still
            # be reaped rather than becoming permanently un-reapable.
            | (
                (LibraryItem.reservation_lease_expires_at.is_(None))
                & (LibraryItem.reserved_at.isnot(None))
                & (LibraryItem.reserved_at < legacy_cutoff)
            )
        )
        .order_by(LibraryItem.id.asc())
        .all()
    ]
    if not stale_ids:
        return 0

    # This candidate list is only a HINT, never authoritative — it was read
    # with NO row lock. `renew_reservation_lease` touches only the item row
    # (by design, so it can never deadlock against this function's user-then-
    # item order — see the module docstring), which means a heartbeat can
    # renew a lease at any point between the unlocked scan above and the
    # `with_for_update()` lock below actually being granted (this function
    # itself is called while the caller already holds `user`'s lock, so a
    # concurrent renewal is never blocked waiting on THIS transaction — only
    # on the specific item row it renews, one at a time). Reaping a row here
    # without re-checking its CURRENT state under lock is the exact
    # renewal-vs-reaper TOCTOU Hermes's third audit found: a renewal that
    # lands in that window would otherwise be silently discarded and the
    # item incorrectly released out from under active, checked-in work.
    candidates = (
        db.query(LibraryItem)
        .filter(LibraryItem.id.in_(stale_ids))
        .populate_existing()
        .with_for_update()
        .order_by(LibraryItem.id.asc())
        .all()
    )
    reap_now = datetime.utcnow()
    reap_legacy_cutoff = reap_now - timedelta(minutes=RESERVATION_TTL_MINUTES)
    stale = []
    for it in candidates:
        # Re-evaluate every condition against the FRESH, lock-protected
        # values `.populate_existing()` just re-read from Postgres — not the
        # unlocked snapshot from the scan above.
        if it.entitlement_status != "pending":
            continue  # already finalized/released/reaped by someone else
        if it.reservation_lease_expires_at is not None:
            if it.reservation_lease_expires_at >= reap_now:
                continue  # renewed since the scan — genuinely still active
        elif it.reserved_at is None or it.reserved_at >= reap_legacy_cutoff:
            continue
        stale.append(it)
    if not stale:
        return 0

    for it in stale:
        it.entitlement_status = "released"
        it.processing_error = it.processing_error or (
            "This took too long to process and was released — try uploading it again."
        )
    db.query(User).filter(User.id == user.id).update(
        {"reserved_sources_count": User.reserved_sources_count - len(stale)},
        synchronize_session="fetch",
    )
    logger.info("Reaped %d stale reservation(s) for user %s", len(stale), user.id)
    return len(stale)


def reap_all_stale_reservations(db: Session, batch_size: int = 25) -> tuple:
    """Global scheduled sweep for stale 'pending' reservations (Task 15
    remediation, Aug 2026) — parity with retry_item_deletions/
    retry_account_erasures/retry_cleanup_tasks/retry_reconciliation_tasks,
    all of which already run on the same 5-minute maintenance cycle
    (notification_service._run_task2_maintenance_cycle).

    Without this, `_reap_stale_reservations` above only ever runs LAZILY,
    triggered by that SAME account's own next `reserve_free_capacity` call
    — a user whose upload worker crashed or timed out, and who never
    uploads again, would have that source sit showing "processing"
    indefinitely, with no other code path that ever revisits it. This is
    purely an operational/UX fix, not an entitlement fix: a stuck 'pending'
    row was already correctly excluded from `successful_sources_total`
    (the one PERMANENT counter — see `can_accept_new_upload`) regardless of
    whether or when it gets reaped, so this sweep cannot change any
    entitlement outcome, only how promptly a stuck row's slot and error
    message become honest.

    Global lock order: user row first, then item row(s) — same convention
    as every other function in this module, enforced by delegating the
    actual reap to `_reap_stale_reservations` itself once each candidate
    user's row is locked. Each affected user is independent, wrapped in its
    own try/except (one user's failure must never block another's), same
    never-collapse-to-one-number `(users_reaped, items_reaped)` contract as
    every other autonomous pass here."""
    now = datetime.utcnow()
    legacy_cutoff = now - timedelta(minutes=RESERVATION_TTL_MINUTES)
    # HINT only, no lock — exactly the same caveat _reap_stale_reservations'
    # own unlocked scan documents; every candidate is re-verified under the
    # user's lock inside _reap_stale_reservations before anything is mutated.
    candidate_user_ids = [
        uid for (uid,) in db.query(LibraryItem.user_id)
        .filter(LibraryItem.entitlement_status == "pending")
        .filter(
            (
                (LibraryItem.reservation_lease_expires_at.isnot(None))
                & (LibraryItem.reservation_lease_expires_at < now)
            )
            | (
                (LibraryItem.reservation_lease_expires_at.is_(None))
                & (LibraryItem.reserved_at.isnot(None))
                & (LibraryItem.reserved_at < legacy_cutoff)
            )
        )
        .distinct()
        .limit(batch_size)
        .all()
    ]

    users_reaped, items_reaped = 0, 0
    for uid in candidate_user_ids:
        user = (
            db.query(User).filter(User.id == uid)
            .populate_existing().with_for_update().first()
        )
        if not user:
            continue  # orphaned rows (e.g. mid account-erasure) — nothing to lock against
        try:
            count = _reap_stale_reservations(db, user)
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("autonomous stale-reservation reap failed for user %s", uid)
            continue
        if count:
            users_reaped += 1
            items_reaped += count

    return users_reaped, items_reaped


def reserve_free_capacity(db: Session, item: LibraryItem, user_id: str) -> bool:
    """Must be called before ANY paid embedding/indexing/OCR work begins for
    `item`. Premium/trial/complimentary users are marked 'premium' and
    always accepted. For Free users, atomically reserves one of the ≤3
    lifetime slots under the global lock order (user row first, then this
    item's row), so a burst of concurrent uploads at 2/3 cannot all pay for
    processing when only one of them can ever be kept.

    Idempotent: retrying for an item that already has an active reservation
    ('pending'/'consumed'/'premium') is a no-op success. Returns False (and
    marks the item 'rejected' with a user-facing message) when capacity is
    genuinely full; callers MUST skip the paid call entirely in that case.

    A NEW `reservation_lease_token` is minted every time this item
    transitions INTO 'pending' (first reservation, or a retry after a reap)
    — read it back via `item.reservation_lease_token` after this returns
    True (the passed-in `item` and the internally-locked row are the SAME
    Python object under SQLAlchemy's identity map, so this attribute is
    already correct without a manual refresh). Callers that do real,
    checkpointed work (OCR) should hold onto that token and renew the lease
    via `renew_reservation_lease` on every checkpoint.
    """
    user = (
        db.query(User).filter(User.id == user_id)
        .populate_existing().with_for_update().first()
    )
    if not user:
        return False

    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item.id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item:
        return False
    if locked_item.entitlement_status in _ACTIVE_RESERVATION_STATES:
        return True

    _reap_stale_reservations(db, user)

    if user.effective_premium:
        locked_item.entitlement_status = "premium"
        locked_item.reservation_lease_token = None
        locked_item.reservation_lease_expires_at = None
        # Premium-created items have no CAPACITY lease (nothing to reap —
        # uncapped), but they still need a durable attempt identity: the
        # item's fixed S3 archive key and its deterministic Pinecone vector
        # ids are overwritten in place by any retry, so compensating cleanup
        # for a stale Premium-created attempt needs the same "am I still the
        # current owner" proof a Free attempt gets from its lease token.
        locked_item.last_processing_attempt_id = str(uuid.uuid4())
        db.commit()
        return True

    if user.successful_sources_total + user.reserved_sources_count >= free_source_limit():
        locked_item.entitlement_status = "rejected"
        locked_item.processing_error = _free_limit_message()
        db.commit()
        return False

    db.query(User).filter(User.id == user_id).update(
        {"reserved_sources_count": User.reserved_sources_count + 1},
        synchronize_session="fetch",
    )
    now = datetime.utcnow()
    token = str(uuid.uuid4())
    locked_item.entitlement_status = "pending"
    locked_item.reserved_at = now
    locked_item.reservation_lease_token = token
    locked_item.reservation_lease_expires_at = now + timedelta(minutes=RESERVATION_TTL_MINUTES)
    # Same value as the lease token — one durable identity per attempt, not
    # two unrelated random values to keep in sync.
    locked_item.last_processing_attempt_id = token
    db.commit()
    return True


def renew_reservation_lease(db: Session, item_id: str, user_id: str, lease_token: str) -> bool:
    """Heartbeat for a live worker doing checkpointed paid work (OCR
    page-by-page progress) against a 'pending' reservation. Extends the
    lease by RESERVATION_TTL_MINUTES from NOW — not from the original
    reservation time — so active work never runs out of lease just because
    the TOTAL job duration exceeds the default window.

    Only ever locks the item row (no user lock — this never writes a `users`
    column), which is deliberately safe under the global lock order: a
    transaction holding only one resource type can never participate in a
    two-resource deadlock.

    Returns False — and the caller MUST treat this as "stop, this attempt no
    longer owns the reservation" — when the item is gone, no longer
    'pending', or `lease_token` no longer matches what's stored (the
    reservation was reaped and a NEWER attempt already owns a fresh token).
    Continuing to pay for more OCR pages after a False here is exactly the
    runaway-cost bug this primitive exists to prevent.
    """
    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item_id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item:
        return False
    if locked_item.entitlement_status != "pending":
        return False
    if locked_item.reservation_lease_token != lease_token:
        return False
    locked_item.reservation_lease_expires_at = datetime.utcnow() + timedelta(minutes=RESERVATION_TTL_MINUTES)
    db.commit()
    return True


# Renewal cadence for a LIVE worker-attempt claim — independent of, and
# deliberately the same magnitude as, RESERVATION_TTL_MINUTES; a shorter
# value would reap a genuinely active worker between heartbeats for no
# reason, a much longer one would let a truly abandoned admission block a
# legitimate retry for too long.
WORKER_ATTEMPT_TTL_MINUTES = 30


def admit_worker_attempt(db: Session, item_id: str, user_id: str) -> "str | None":
    """Task 2 final consolidated backend pass (Verified Blocker 1) —
    atomically admits ONE new worker-attempt invocation for `item_id`,
    layered on top of (never replacing) Free-capacity reservation. Call
    this AFTER `reserve_free_capacity` has already secured capacity (Free)
    or after the item is legitimately 'premium' — this function does not
    check or touch capacity at all, only worker-attempt admission, so a
    rejection here never consumes or releases another worker's Free-
    capacity reservation.

    Rejects (returns None), atomically under a row lock, before any
    external/paid action, when:
      - the item does not exist for this owner;
      - the item is tombstoned (`deletion_state is not None`);
      - the item is already fully processed — replaying a worker after
        successful completion must not re-admit and re-pay;
      - a LIVE worker attempt already owns the item (its lease has not
        expired) — a second, competing caller is rejected and NEVER
        receives that attempt's id; it cannot share or copy it.

    On success, mints a fresh, immutable attempt id and stamps it onto
    BOTH `worker_attempt_id` (the live-claim field `renew_worker_attempt`/
    `release_worker_attempt` manage) and `last_processing_attempt_id` (the
    durable cleanup/vector/archive-scoping identity the rest of this
    codebase already keys off) — independent of any Free-capacity lease,
    so a later legitimate continuation (e.g. OCR after a PDF/EPUB
    extraction found no text) gets its OWN new worker attempt while the
    SAME capacity reservation, if any, is untouched."""
    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item_id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item:
        return None
    if locked_item.deletion_state is not None:
        return None
    if locked_item.processed:
        return None
    now = datetime.utcnow()
    live = (
        locked_item.worker_attempt_id is not None
        and locked_item.worker_attempt_expires_at is not None
        and locked_item.worker_attempt_expires_at > now
    )
    if live:
        return None
    new_attempt_id = str(uuid.uuid4())
    locked_item.worker_attempt_id = new_attempt_id
    locked_item.worker_attempt_expires_at = now + timedelta(minutes=WORKER_ATTEMPT_TTL_MINUTES)
    locked_item.last_processing_attempt_id = new_attempt_id
    db.commit()
    return new_attempt_id


def renew_worker_attempt(db: Session, item_id: str, user_id: str, attempt_id: str) -> bool:
    """The tier-agnostic heartbeat for a LIVE worker-attempt claim —
    extends the lease by WORKER_ATTEMPT_TTL_MINUTES from NOW. Returns
    False when the item is gone or `attempt_id` no longer matches the
    item's CURRENT `worker_attempt_id` (superseded by a newer admission);
    the caller MUST treat False as ownership lost."""
    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item_id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item or locked_item.worker_attempt_id != attempt_id:
        return False
    locked_item.worker_attempt_expires_at = datetime.utcnow() + timedelta(minutes=WORKER_ATTEMPT_TTL_MINUTES)
    db.commit()
    return True


def release_worker_attempt(db: Session, item_id: str, user_id: str, attempt_id: str) -> None:
    """Clears the live-claim fields ONLY while `attempt_id` is still the
    current owner — a stale attempt's own release can never clear a newer
    attempt's claim. Called on every terminal outcome (success or
    failure) so a legitimate later continuation (e.g. OCR after empty-
    text extraction) can be re-admitted afterward."""
    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item_id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item or locked_item.worker_attempt_id != attempt_id:
        return
    locked_item.worker_attempt_id = None
    locked_item.worker_attempt_expires_at = None
    db.commit()


def release_reservation(
    db: Session, item: LibraryItem, user_id: str, reason: str = None, lease_token: str = None,
) -> None:
    """Processing failed (or was abandoned) after a reservation was made —
    return the slot to available capacity. Idempotent and safe to call
    unconditionally from every processing function's except-block: a no-op
    for an item that was never reserved, is 'premium' (nothing to release),
    was already finalized/released, or (crucially) no longer EXISTS — a
    concurrent deletion racing this call is exactly why the item lookup
    happening under the global lock order and simply finding nothing is
    correct, not an error.

    `lease_token`, when given, must match the item's CURRENT token or this
    is a no-op: releasing a reservation this caller no longer owns (it was
    reaped and re-reserved under a newer token) would incorrectly decrement
    `reserved_sources_count` for the NEW attempt's still-active reservation.
    Callers that never tracked a token (most of the plain embedding paths,
    where a single atomic call either fully succeeds or fully fails with no
    intermediate checkpoint to steal) pass `None`, which skips this check —
    an accepted, documented scope boundary; see the completion report.
    """
    user = (
        db.query(User).filter(User.id == user_id)
        .populate_existing().with_for_update().first()
    )
    if not user:
        return
    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item.id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item or locked_item.entitlement_status != "pending":
        return
    if lease_token is not None and locked_item.reservation_lease_token != lease_token:
        return
    locked_item.entitlement_status = "released"
    locked_item.reservation_lease_token = None
    locked_item.reservation_lease_expires_at = None
    if reason:
        locked_item.processing_error = reason
    db.query(User).filter(User.id == user_id).update(
        {"reserved_sources_count": User.reserved_sources_count - 1},
        synchronize_session="fetch",
    )
    db.commit()


def touch_last_active(item: LibraryItem) -> None:
    """Stamp the authoritative "actually used" signal. Deliberately separate
    from `updated_at`. Caller commits."""
    item.last_active_at = datetime.utcnow()


# ── Downgrade lock selection ─────────────────────────────────────────────────

def _entitlement_token(user: User) -> str:
    """Identifies the entitlement "episode" the persisted selection was
    computed for. See `reconcile_free_lock_state` for the wall-clock case
    this alone does not catch."""
    until = user.premium_until.isoformat() if user.premium_until else "-"
    return f"{'P' if user.is_premium else '-'}:{until}"


def _fallback_candidates(db: Session, user: User, limit: int) -> list:
    """Deterministic "three most recently active" fallback. Successfully
    processed only."""
    return (
        db.query(LibraryItem)
        .filter(
            LibraryItem.user_id == user.id,
            LibraryItem.processed.is_(True),
            LibraryItem.deletion_state.is_(None),  # Task 2 closeout: a tombstoned item cannot be selected
        )
        .order_by(
            LibraryItem.is_active.desc(),
            LibraryItem.last_active_at.desc().nullslast(),
            LibraryItem.created_at.desc(),
            LibraryItem.id.asc(),
        )
        .limit(limit)
        .all()
    )


def _current_valid_selection(db: Session, user: User) -> list:
    """Whatever is already flagged True right now, restricted to sources
    that are still owned and successfully processed. A deleted source is
    simply absent — no rotation happens to replace it."""
    return (
        db.query(LibraryItem)
        .filter(
            LibraryItem.user_id == user.id,
            LibraryItem.is_unlocked_selection.is_(True),
            LibraryItem.processed.is_(True),
            LibraryItem.deletion_state.is_(None),  # Task 2 closeout: a tombstoned item is not a valid selection
        )
        .all()
    )


def _apply_selection(db: Session, user: User, chosen: dict) -> None:
    """Persist exactly `chosen` ({item_id: selection_kind}) as the ≤3-item
    Free selection, clearing everything else."""
    db.query(LibraryItem).filter(
        LibraryItem.user_id == user.id,
        LibraryItem.is_unlocked_selection.is_(True),
    ).update({"is_unlocked_selection": False, "selection_kind": None}, synchronize_session=False)
    by_kind: dict = {}
    for item_id, kind in chosen.items():
        by_kind.setdefault(kind, []).append(item_id)
    for kind, ids in by_kind.items():
        db.query(LibraryItem).filter(
            LibraryItem.user_id == user.id,
            LibraryItem.id.in_(ids),
        ).update({"is_unlocked_selection": True, "selection_kind": kind}, synchronize_session=False)


def set_explicit_selection(db: Session, user: User, chosen_ids: set) -> None:
    """A currently-entitled user choosing (or changing) which ≤3 sources
    should survive their NEXT downgrade. A pure preference — touches neither
    `is_active`/`free_lock_state_token` NOR entitlement accounting, since
    nothing is being locked (or permanently consumed) right now."""
    _apply_selection(db, user, {i: "explicit" for i in chosen_ids})
    db.commit()


def finalize_lock_selection(db: Session, user: User) -> None:
    """Persist (or confirm) the ≤3-item Free selection for the CURRENT
    entitlement state and stamp the token so this is a no-op until the state
    actually changes again.

    Global lock order: re-locks `user` FIRST (the caller's `user` object may
    be a plain, unlocked load from `get_current_user`), THEN every candidate
    item row, in ascending id order — required because this function can
    both READ available capacity and WRITE new permanent consumption; two
    concurrent downgrade reconciliations for the same account must never
    both observe the same "capacity available" snapshot and both promote,
    which would push the total past the configured limit.

    Downgrade accounting (2nd-audit remediation #1): a retained item whose
    `entitlement_status` is not already 'consumed' (i.e. 'premium', or an
    unattributed pre-cutover 'grandfathered' row) is promoted to 'consumed'
    and counted — this IS what "occupies a permanent Free entitlement"
    means. Promotion is capped by `free_source_limit() -
    successful_sources_total`: it can raise the total up to the limit, never
    past it, and never for a candidate the account cannot currently afford.
    When capacity runs out before every fallback slot is filled, already-
    'consumed' sources (costing nothing new) are preferred to top up the
    retained set over ones that would need new capacity, rather than
    settling for fewer than the account's own existing consumption entitles
    it to.
    """
    locked_user = (
        db.query(User).filter(User.id == user.id)
        .populate_existing().with_for_update().first()
    )
    if not locked_user:
        return
    user = locked_user

    token = _entitlement_token(user)
    limit = free_source_limit()
    existing = _current_valid_selection(db, user)

    ordered_ids: list = []
    seen = set()
    for it in existing[:limit]:
        ordered_ids.append(it.id)
        seen.add(it.id)
    if len(ordered_ids) < limit:
        for cand in _fallback_candidates(db, user, limit + len(ordered_ids)):
            if len(ordered_ids) >= limit:
                break
            if cand.id in seen:
                continue
            ordered_ids.append(cand.id)
            seen.add(cand.id)

    existing_kind = {it.id: it.selection_kind for it in existing}

    candidates = []
    if ordered_ids:
        candidates = (
            db.query(LibraryItem)
            .filter(LibraryItem.id.in_(ordered_ids))
            .populate_existing()
            .with_for_update()
            .order_by(LibraryItem.id.asc())
            .all()
        )
    by_id = {it.id: it for it in candidates}
    ordered_candidates = [by_id[i] for i in ordered_ids if i in by_id]

    capacity_available = max(0, limit - user.successful_sources_total)
    already_consumed = [it for it in ordered_candidates if it.entitlement_status == "consumed"]
    needs_promotion = [it for it in ordered_candidates if it.entitlement_status != "consumed"]
    promote_now = needs_promotion[:capacity_available]

    final = already_consumed + promote_now
    if len(final) < limit and len(needs_promotion) > len(promote_now):
        # Capacity ran out before every fallback slot could be filled.
        # Retaining an un-promotable candidate would create extra capacity
        # (explicitly prohibited) — top up instead with any OTHER already-
        # consumed, currently-owned source not already in the pool. Costs no
        # new capacity, and is strictly better than leaving a slot locked
        # when a no-cost candidate exists.
        already_ids = {it.id for it in final}
        extra_query = db.query(LibraryItem).filter(
            LibraryItem.user_id == user.id,
            LibraryItem.processed.is_(True),
            LibraryItem.entitlement_status == "consumed",
        )
        if already_ids:
            extra_query = extra_query.filter(LibraryItem.id.notin_(already_ids))
        extra = (
            extra_query
            .populate_existing()
            .with_for_update()
            .order_by(
                LibraryItem.last_active_at.desc().nullslast(),
                LibraryItem.created_at.desc(),
                LibraryItem.id.asc(),
            )
            .limit(limit - len(final))
            .all()
        )
        final += extra
    final = final[:limit]

    promote_ids = {it.id for it in promote_now}
    if promote_ids:
        for it in promote_now:
            it.entitlement_status = "consumed"
            it.reservation_lease_token = None
            it.reservation_lease_expires_at = None
        db.query(User).filter(User.id == user.id).update(
            {"successful_sources_total": User.successful_sources_total + len(promote_ids)},
            synchronize_session="fetch",
        )

    chosen = {it.id: (existing_kind.get(it.id) or "fallback") for it in final}

    _apply_selection(db, user, chosen)
    # A source that just became locked must also leave the active line-up.
    locked_filter = [LibraryItem.id.notin_(list(chosen.keys()))] if chosen else []
    db.query(LibraryItem).filter(
        LibraryItem.user_id == user.id,
        LibraryItem.is_active.is_(True),
        *locked_filter,
    ).update({"is_active": False}, synchronize_session=False)
    user.free_lock_state_token = token
    user.free_lock_last_effective_premium = False
    db.commit()


def maybe_auto_unlock_new_source(db: Session, user: User, item: LibraryItem) -> None:
    """A brand-new source that just finished processing while genuinely Free
    joins the persisted selection immediately if there is still room under
    the lifetime limit. Never rotates an existing locked source out; only
    fills a slot that is still genuinely open."""
    if user.effective_premium:
        return
    current = _current_valid_selection(db, user)
    if len(current) >= free_source_limit():
        return
    item.is_unlocked_selection = True
    item.selection_kind = "fallback"
    user.free_lock_state_token = _entitlement_token(user)
    user.free_lock_last_effective_premium = False


def reconcile_free_lock_state(db: Session, user: User) -> None:
    """Lazy, idempotent reconciliation — call before trusting any lock/
    unlock state for this user."""
    if user.effective_premium:
        if user.free_lock_last_effective_premium is not True:
            user.free_lock_last_effective_premium = True
            db.commit()
        return
    token_changed = user.free_lock_state_token != _entitlement_token(user)
    wall_clock_transition = user.free_lock_last_effective_premium is not False
    if token_changed or wall_clock_transition:
        finalize_lock_selection(db, user)


# ── Successful-processing gate (upload completion) ───────────────────────────

def finalize_successful_processing(
    db: Session, item: LibraryItem, user_id: str, chunk_count: int, lease_token: str = None,
    attempt_token: str = None,
) -> bool:
    """Mark `item` processed, exactly once, converting its reservation into
    permanent consumption (Free) or leaving it exempt (Premium-created).
    Returns whether it was accepted.

    Global lock order: user row FIRST, then the item row.

    Concurrency-safe against the SAME item being finalized twice: the row
    lock is acquired, and `processed` is re-read AFTER the lock is held — a
    second worker blocks until the first commits, then sees `processed=True`
    and returns immediately without touching the counter a second time.
    Ownership is re-verified by the same lookup (`user_id` is part of the
    WHERE clause).

    `attempt_token` (Task 2 consolidated backend pass — the IMMUTABLE
    per-tier-agnostic attempt identity, `LibraryItem.last_processing_
    attempt_id`) is checked FIRST, before the `processed` idempotency
    shortcut below — a stale attempt must never be told "yes, accepted"
    merely because SOME other, newer attempt has already set
    `processed=True`; "someone finished" is not proof that THIS caller is
    the one who did. This is what makes stale-Premium rejection identical
    to stale-Free rejection: Premium attempts have no `lease_token` at all
    (always `None`), so `attempt_token` is the ONLY ownership signal that
    tier has, and it is now checked unconditionally whenever the caller
    supplies one — Premium ownership is no longer silently skipped.

    `lease_token`, when given, is ADDITIONALLY verified against the item's
    CURRENT 'pending' token before conversion (the Free-capacity-specific
    check) — a worker whose reservation has been superseded (reaped and
    re-reserved under a newer token) gets False and MUST NOT be treated as
    having consumed anything; the caller is expected to run compensating
    cleanup (see app/routers/library.py). Callers that never tracked either
    token pass `None`/`None`, which skips both checks — a documented,
    narrow scope boundary (see `release_reservation`).

    `.populate_existing()` on every locked query in this module is NOT
    decorative — see the historical note in the completion report: without
    it, SQLAlchemy's identity map silently serves a pre-lock Python object
    even though the `SELECT ... FOR UPDATE` genuinely executed and blocked
    at the Postgres level, which reproduced Hermes's original same-item
    double-count exactly.
    """
    user = (
        db.query(User).filter(User.id == user_id)
        .populate_existing().with_for_update().first()
    )
    if not user:
        return False

    locked_item = (
        db.query(LibraryItem)
        .filter(LibraryItem.id == item.id, LibraryItem.user_id == user_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if not locked_item:
        return False

    if locked_item.deletion_state is not None:
        # Task 2 closeout (Verified Blocker 6, real regression found under
        # 2nd-run PostgreSQL concurrency verification — PG13d): DELETE's
        # PHASE 1 commits fast (decrementing reserved_sources_count,
        # stamping deletion_state='pending') and then runs its slower
        # external-cleanup PHASE 2 as a SEPARATE transaction — deliberately,
        # so accepting a deletion never blocks on Pinecone/S3. That leaves
        # a real gap between phase 1's commit and phase 2's own locks
        # during which `entitlement_status` is UNTOUCHED ('pending'), so a
        # concurrent finalize — which never checked deletion_state — could
        # win the SAME row's lock in that gap and decrement
        # reserved_sources_count a SECOND time for the identical
        # reservation, going negative. A tombstoned item must never be
        # finalized (no paid/provider action's accounting may land on one),
        # so this refuses unconditionally, before any lease/attempt check.
        return False

    if attempt_token is not None and locked_item.last_processing_attempt_id != attempt_token:
        logger.warning(
            "finalize_successful_processing: item %s presented a superseded "
            "attempt id (current owner=%r, caller=%r) — refusing, regardless "
            "of whether a DIFFERENT attempt has already finalized this item",
            item.id, locked_item.last_processing_attempt_id, attempt_token,
        )
        return False

    # A caller presenting a lease_token but NO attempt_token has no way to
    # prove it's the SAME attempt that may have already finalized this item
    # (that proof is exactly what the attempt_token check above provides,
    # and it was skipped here because attempt_token was None) — so the
    # `processed` idempotency shortcut immediately below must not be
    # allowed to wave a stale lease_token through unchecked. Validate it
    # HERE, before that shortcut, using the same "current owner" test the
    # normal (not-yet-processed) lease_token check below already uses.
    # Every current production caller passes both tokens together (so this
    # branch is unreached in practice today), but the primitive itself
    # must not depend on that discipline to stay correct — found by an
    # independent audit's adversarial harness (tests/test_task2_attempt_
    # lifecycle_repro.py, section M): calling this function directly with
    # only a stale lease_token, after a different newer attempt had
    # already finalized the item, incorrectly returned True.
    if lease_token is not None and attempt_token is None:
        if locked_item.entitlement_status != "pending" or locked_item.reservation_lease_token != lease_token:
            logger.warning(
                "finalize_successful_processing: item %s presented a lease "
                "token with no attempt_token to verify identity, and the "
                "token no longer matches the current owner — refusing an "
                "unverifiable retry rather than trusting the `processed` "
                "shortcut below", item.id,
            )
            return False

    if locked_item.processed:
        return True  # idempotent retry by the SAME, still-current attempt

    status = locked_item.entitlement_status
    if lease_token is not None:
        # A caller presenting a token is claiming ownership of one SPECIFIC
        # 'pending' attempt. If the item is no longer 'pending' under that
        # EXACT token (reaped, released, rejected, or already finalized by
        # someone else), this attempt has been superseded — return False
        # immediately WITHOUT falling through to the defensive on-the-fly
        # reservation below. Falling through would let a stale worker's
        # invalidated attempt silently mint a FRESH reservation and consume
        # it, which defeats the entire point of the lease token: it must
        # never be possible for a superseded attempt to still succeed just
        # because it retries the same call.
        if status != "pending" or locked_item.reservation_lease_token != lease_token:
            logger.warning(
                "finalize_successful_processing: item %s presented a superseded "
                "lease token — refusing to convert a reservation this caller no "
                "longer owns", item.id,
            )
            return False

    if status not in ("pending", "premium"):
        # Defensive: every processing path should have called
        # reserve_free_capacity before reaching here.
        logger.warning(
            "finalize_successful_processing: item %s reached completion with no "
            "reservation (status=%r) — reserving on the fly", item.id, status,
        )
        if not reserve_free_capacity(db, locked_item, user_id):
            return False
        locked_item = (
            db.query(LibraryItem)
            .filter(LibraryItem.id == item.id, LibraryItem.user_id == user_id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        status = locked_item.entitlement_status

    locked_item.processed = True
    locked_item.chunk_count = chunk_count
    locked_item.processing_error = None
    locked_item.reservation_lease_token = None
    locked_item.reservation_lease_expires_at = None

    if status == "pending":
        db.query(User).filter(User.id == user_id).update(
            {
                "reserved_sources_count": User.reserved_sources_count - 1,
                "successful_sources_total": User.successful_sources_total + 1,
            },
            synchronize_session="fetch",
        )
        locked_item.entitlement_status = "consumed"
        maybe_auto_unlock_new_source(db, user, locked_item)
    else:  # 'premium' — created while entitled; never touches the Free counter
        locked_item.entitlement_status = "premium"

    db.commit()
    return True


# ── Migration / backfill for existing accounts ───────────────────────────────

def _backfill_one(db: Session, user: User) -> None:
    """Pre-cutover accounts have no reservation history. Documented,
    user-favorable policy (Task 2 remediation #15):

      · The SAME ≤3 "most recently active" sources the existing lock-
        selection mechanism already picks become the permanently-consumed
        set (`successful_sources_total = len(that set)`, never more than
        the limit), regardless of the account's CURRENT tier.
      · Every OTHER pre-cutover successfully-processed source is
        'grandfathered' — stored, still visible, still selectable later
        (and promotable into permanent consumption at a FUTURE live
        downgrade if capacity allows — see finalize_lock_selection), but
        never counted as a consumed lifetime slot by the migration itself.
    """
    if user.free_lock_state_token is not None:
        return  # already reconciled (live traffic or an earlier backfill pass)

    limit = free_source_limit()
    chosen = {it.id: "legacy_fallback" for it in _fallback_candidates(db, user, limit)}
    _apply_selection(db, user, chosen)

    processed_ids = {
        pid for (pid,) in db.query(LibraryItem.id)
        .filter(LibraryItem.user_id == user.id, LibraryItem.processed.is_(True))
        .all()
    }
    grandfathered = processed_ids - set(chosen.keys())
    if grandfathered:
        db.query(LibraryItem).filter(LibraryItem.id.in_(grandfathered)).update(
            {"entitlement_status": "grandfathered"}, synchronize_session=False,
        )
    if chosen:
        db.query(LibraryItem).filter(LibraryItem.id.in_(chosen.keys())).update(
            {"entitlement_status": "consumed"}, synchronize_session=False,
        )

    user.successful_sources_total = len(chosen)
    user.reserved_sources_count = 0
    user.free_lock_state_token = _entitlement_token(user)
    user.free_lock_last_effective_premium = bool(user.effective_premium)

    if not user.effective_premium:
        locked_filter = [LibraryItem.id.notin_(chosen.keys())] if chosen else []
        db.query(LibraryItem).filter(
            LibraryItem.user_id == user.id,
            LibraryItem.is_active.is_(True),
            *locked_filter,
        ).update({"is_active": False}, synchronize_session=False)

    db.commit()


def backfill_existing_accounts(db: Session) -> tuple:
    """Migration-time pass for every account that predates Task 2. Safe to
    call on every boot. Returns `(ok_count, failed_count)` — NEVER a single
    count that could misrepresent a partial failure as full success."""
    users = db.query(User).filter(User.free_lock_state_token.is_(None)).all()
    ok, failed = 0, 0
    for user in users:
        try:
            _backfill_one(db, user)
            ok += 1
        except Exception:
            db.rollback()
            failed += 1
            logger.exception("entitlement backfill failed for user %s", user.id)
    return ok, failed


# ── Mixed-version cutover reconciliation (Task 2, 3rd-audit remediation #10) ─
#
# `backfill_existing_accounts` above is a ONE-TIME, PER-ACCOUNT pass — once
# `free_lock_state_token` is set, `_backfill_one` never revisits that account
# again. That is exactly right for a normal boot, but it does NOT cover a
# genuinely new failure mode a rolling deploy can introduce: an OLD-code
# worker (still running the pre-remediation `process_*_embeddings`/`_run_ocr`,
# mid-flight when the new backend started) can finish AFTER the new schema and
# backfill are already live, and old code simply does `item.processed = True;
# db.commit()` — it has never heard of `entitlement_status`,
# `reservation_lease_token`, or any of this file's accounting. The row it
# leaves behind is unmistakable: `processed = True` with `entitlement_status
# IS NULL`, on an account that's ALREADY reconciled (so `_backfill_one` will
# never look at it again).
#
# Rather than trying to make an OLD PROCESS somehow cooperate with a fencing
# protocol it was never built to understand (the "database-enforced
# writer-version fencing" option Hermes's audit allowed, but not the one
# chosen here — see the completion report's rollout runbook for why a short
# drain window plus this reconciliation pass is the simpler, equally-safe
# choice for a single-process Railway deployment with no separate worker
# fleet), this function finds and repairs exactly that signature — using the
# IDENTICAL, already-shipped, user-favorable policy `_backfill_one` uses for
# pre-cutover accounts: promote into permanent consumption if the owning
# account still has room, capped at the limit; grandfather (stored, visible,
# never counted) if it doesn't. It is deliberately ITEM-scoped, not
# account-scoped, so it can run safely on every boot alongside
# backfill_existing_accounts without needing to know which accounts are
# "already done" — an item either still shows the old-worker signature or it
# doesn't, which makes repeated runs naturally idempotent.
def _reconcile_one_unaccounted_item(db: Session, item: LibraryItem) -> None:
    user = (
        db.query(User).filter(User.id == item.user_id)
        .populate_existing().with_for_update().first()
    )
    if not user:
        return
    locked_item = (
        db.query(LibraryItem).filter(LibraryItem.id == item.id)
        .populate_existing().with_for_update().first()
    )
    if not locked_item or not locked_item.processed or locked_item.entitlement_status not in (None, "released"):
        return  # already reconciled by a concurrent pass, deleted, or not the signature at all

    if user.effective_premium:
        locked_item.entitlement_status = "premium"
        db.commit()
        return

    limit = free_source_limit()
    if user.successful_sources_total < limit:
        locked_item.entitlement_status = "consumed"
        db.query(User).filter(User.id == user.id).update(
            {"successful_sources_total": User.successful_sources_total + 1},
            synchronize_session="fetch",
        )
    else:
        # No room left — grandfathered, exactly like a pre-cutover excess
        # source: stored, visible, selectable later, never counted now.
        locked_item.entitlement_status = "grandfathered"
    db.commit()


def reconcile_unaccounted_processed_items(db: Session) -> tuple:
    """Boot-time (and safe to call any time) sweep for the mixed-version
    cutover signature: `processed = True` with either `entitlement_status
    IS NULL` (a legacy row from before the database fencing trigger
    existed) or `entitlement_status == 'released'` (Task 2 lifecycle
    remediation, Follow-up 2A — the CONTINUOUS database trigger in
    app/database.py fences exactly this signature to 'released' the
    instant it's written, by any process, at any time — so this sweep no
    longer needs to be the ONLY thing that ever sees the dangerous window;
    it's what promotes a fenced row into real accounting). Returns
    `(fixed_count, failed_count)` — same never-collapse-to-one-number
    contract as `backfill_existing_accounts`. Idempotent: an item this
    already fixed no longer matches the filter, so re-running finds nothing
    left to do — which IS the go/no-go check (see the completion report):
    `SELECT count(*) FROM library_items WHERE processed AND
    entitlement_status IN (NULL, 'released')` must be 0 after this runs clean.
    """
    items = (
        db.query(LibraryItem)
        .filter(
            LibraryItem.processed.is_(True),
            (LibraryItem.entitlement_status.is_(None)) | (LibraryItem.entitlement_status == "released"),
        )
        .order_by(LibraryItem.id.asc())
        .all()
    )
    fixed, failed = 0, 0
    for item in items:
        try:
            _reconcile_one_unaccounted_item(db, item)
            fixed += 1
        except Exception:
            db.rollback()
            failed += 1
            logger.exception("unaccounted-item reconciliation failed for item %s", item.id)
    return fixed, failed


# ── Autonomous durable-cleanup retry (Task 2 lifecycle remediation, ────────
# Follow-up 2A) ──────────────────────────────────────────────────────────
#
# app/routers/library.py's compensating-cleanup helpers (`_compensate_
# failed_attempt` and friends) always write DURABLE state into the
# `cleanup_tasks` ledger (see app/models/library.py's `CleanupTask`) BEFORE
# attempting a Pinecone/S3 delete, and leave that record `cleanup_state ==
# 'failed'` — retaining the item id, user id, attempt token, artifact kind,
# and exact artifact key — on an exception OR an ordinary `False` return
# from the provider. Until this function existed, nothing ever revisited a
# 'failed' record except a brand-new attempt for the SAME item calling
# `_compensate_failed_attempt()` again directly — a genuinely stale
# attempt's failure, once written, sat there forever with no autonomous
# path back to a real retry.
#
# This is that autonomous path: a real scan/runner, callable with only
# `db` (no item/user/attempt argument — it discovers eligible work itself),
# wired to run at application startup (see app/database.py's
# `_run_task2_required_migrations_and_backfill`) and safe to call again at
# any time (an operator console, a future scheduled job, or — as here — a
# test harness proving it resolves durable failures without any item-
# specific direct compensation call).
def retry_cleanup_tasks(db: Session, batch_size: int = 25) -> tuple:
    """Scan `cleanup_tasks` for 'pending'/'failed' records not currently
    claimed by another live runner, retry each one's exact provider
    delete, and resolve it on success. Returns `(resolved_count,
    failed_count)` — same never-collapse-to-one-number contract as every
    other reconciliation pass in this module.

    Claiming (requirement: "claim work safely against concurrent
    runners"): each candidate row is re-read and row-locked
    (`with_for_update()`) individually, its claim stamped
    (`claimed_by`/`claimed_until`, a short lease) and committed BEFORE the
    provider call, and re-verified under lock again before the resolution
    write — so two runners racing the same batch retry disjoint rows
    rather than double-processing one, and a runner that crashes mid-
    retry simply leaves its claim to expire, after which the row becomes
    eligible again (idempotent: retrying an already-resolved kind of
    delete is a safe no-op at the provider, and this function never
    re-resolves an already-'resolved' row since the eligibility filter
    excludes it).

    Retry is always scoped to the EXACT (item, user, attempt, artifact
    key) identity stored on the claimed row — never a newer attempt's
    object, and never a different item's, because that identity is
    everything the provider calls below are given."""
    from datetime import datetime as _dt, timedelta as _td
    from app.models.library import CleanupTask
    # Deliberately routed through app.routers.library's OWN module
    # namespace (a lazy, function-local import — entitlement_service.py has
    # no module-level dependency on the routers package, avoiding a
    # circular import) rather than importing EmbeddingService/S3Service
    # fresh from their origin modules: every provider-facing helper this
    # runner mirrors (`_compensate_failed_attempt` and friends) is already
    # driven through that exact namespace, so referencing it here too means
    # this runner is provider-substitutable through the SAME seam the rest
    # of the ingestion lifecycle already uses — not a second, independent
    # binding a caller would have to know to patch separately.
    from app.routers import library as _library_router

    runner_id = str(uuid.uuid4())
    claim_ttl = _td(minutes=5)
    now = _dt.utcnow()

    candidate_ids = [
        tid for (tid,) in db.query(CleanupTask.id)
        .filter(
            CleanupTask.cleanup_state.in_(("pending", "failed")),
            (CleanupTask.claimed_until.is_(None)) | (CleanupTask.claimed_until < now),
        )
        .order_by(CleanupTask.updated_at.asc())
        .limit(batch_size)
        .all()
    ]

    resolved, failed = 0, 0
    for task_id in candidate_ids:
        task = (
            db.query(CleanupTask).filter(CleanupTask.id == task_id)
            .populate_existing().with_for_update().first()
        )
        if not task or task.cleanup_state not in ("pending", "failed"):
            continue  # resolved (or no longer eligible) since the scan above
        if task.claimed_until is not None and task.claimed_until >= _dt.utcnow():
            continue  # a concurrent runner already holds this exact row
        task.claimed_by = runner_id
        task.claimed_until = _dt.utcnow() + claim_ttl
        db.commit()

        ok = False
        error_detail = None
        try:
            if task.artifact_kind == "vectors":
                result = _library_router.EmbeddingService().delete_item_vectors(
                    task.item_id, user_id=task.user_id, attempt_token=task.attempt_token,
                )
                ok = bool(result)
                if not ok:
                    error_detail = "delete_item_vectors returned False"
            elif task.artifact_kind in ("s3", "s3_image") and task.artifact_key:
                result = _library_router.S3Service().delete_file(task.artifact_key)
                ok = bool(result)
                if not ok:
                    error_detail = "delete_file returned False"
            else:
                # An unrecognized artifact_kind, or an 's3'/'s3_image'
                # record with no key — Task 2 consolidated backend pass
                # (correction): this is NOT "nothing meaningful left to
                # retry", it is a malformed/incomplete record that must
                # stay failed and operator-visible, never silently marked
                # resolved with zero provider calls.
                ok = False
                error_detail = (
                    f"unrecognized artifact_kind {task.artifact_kind!r} or missing "
                    f"required artifact_key — remains failed and retryable"
                )
        except Exception as e:
            ok = False
            error_detail = str(e)[:250]
            logger.exception(
                "cleanup-task retry failed for task %s (item %s, kind %s)",
                task.id, task.item_id, task.artifact_kind,
            )

        # Delegate the actual resolve to the SAME helper the direct
        # compensation path uses (`app.routers.library._cleanup_ledger_
        # resolve`) — the one place that knows how to keep the ledger row
        # AND its `LibraryItem.cleanup_state`/`cleanup_detail` mirror (for
        # a still-current-owner attempt) consistent with each other.
        # Duplicating that logic here would let the two representations
        # drift apart the moment a task is resolved by THIS runner instead
        # of by a live worker's own compensation call.
        # `expected_claimed_by=runner_id` (Task 2 consolidated backend
        # pass, correction) — a LATE-resolving runner whose claim has
        # since expired and been reclaimed by a newer runner must not
        # overwrite that newer runner's own, already-authoritative
        # result. `_cleanup_ledger_resolve` re-checks the row's CURRENT
        # `claimed_by` under lock and silently no-ops if it no longer
        # matches this exact runner_id. `artifact_key=task.artifact_key`
        # (Task 2 closeout, Verified Blocker 4) identifies the EXACT row
        # now that one attempt can hold several independent 's3_image'
        # records at once.
        applied = _library_router._cleanup_ledger_resolve(
            task.item_id, task.attempt_token, task.artifact_kind, ok, error_detail,
            expected_claimed_by=runner_id, artifact_key=task.artifact_key,
        )
        # Task 2 closeout (Verified Blocker 4): count resolved/failed ONLY
        # for a transition THIS runner's claim actually committed — if a
        # newer runner reclaimed the row first, `applied` is False and
        # this runner's own (possibly stale) `ok` value must not be
        # counted at all, success or failure.
        if applied:
            if ok:
                resolved += 1
            else:
                failed += 1

        # Release ONLY this runner's own claim lease — bookkeeping private
        # to this runner, which _cleanup_ledger_resolve has no reason to
        # know about. Guarded by claimed_by == runner_id (Task 2
        # consolidated backend pass, correction): if a newer runner has
        # since reclaimed this row, clearing its claim here would release
        # that runner's lease early, letting a THIRD runner double-claim
        # a row the second runner may still be actively working.
        fresh = (
            db.query(CleanupTask).filter(CleanupTask.id == task_id)
            .populate_existing().with_for_update().first()
        )
        if fresh and fresh.claimed_by == runner_id:
            fresh.claimed_by = None
            fresh.claimed_until = None
            db.commit()

    return resolved, failed


# ── Mixed-version cutover: autonomous reconciliation worker (Task 2 ────────
# consolidated backend pass, Option B) ──────────────────────────────────────
#
# The database fencing trigger (app/database.py, _ensure_mixed_version_
# fencing) now fences an old-writer's `processed=True` write to
# `processed=False` / `entitlement_status='released'` IMMEDIATELY, and
# durably records the exact fencing event (via a `reconciliation_
# generation` stamped on the item, matched on a `ReconciliationTask` row)
# in the SAME transaction. This is that task's autonomous resolver: real
# capacity accounting, restoring the item's correct terminal state,
# processed by this repo's production scheduler on its own recurring tick
# — never gated on the next process restart.
def _resolve_one_reconciliation_task(db: Session, task, runner_id: str) -> bool:
    """Task 2 final consolidated backend pass (Verified Blocker 5): fixed
    lock order. The PRIOR version of this function locked the item FIRST
    and the user SECOND — the exact opposite of this module's documented
    global order (user row first, then item row) — creating a real
    deadlock hazard against every other function here (finalize_
    successful_processing, reserve_free_capacity, etc.) that locks user
    then item.

    Correct order: determine the owning user with a PLAIN read (no lock
    retained), lock the user, THEN lock the item — never holding an item
    lock while about to acquire a user lock.

    Also re-verifies the reconciliation task's OWN claim (claimed_by /
    claimed_until) under a fresh lock immediately before mutating
    anything: `retry_reconciliation_tasks` commits after claiming (which
    releases that row's lock), so without this re-check here, an expired
    Runner A that got this far before its claim lapsed could still mutate
    the item after Runner B legitimately reclaimed the same task — this
    closes that window.

    Verifies the task's `generation` still matches the item's CURRENT
    `reconciliation_generation` before doing anything, and — unlike
    `_reconcile_one_unaccounted_item`, which requires `processed=True` —
    explicitly supports the fenced `processed=False` signature this task
    describes. Returns True when the task is fully resolved (including
    the legitimate "there is nothing left to do" cases: item gone,
    generation superseded, already reconciled by a concurrent pass, or
    claim lost), False on a genuine failure that should remain
    retryable."""
    from app.models.library import ReconciliationTask

    # Plain, non-locking read — just enough to know which user to lock
    # first. Never upgrade this to with_for_update(): holding the item
    # lock here, before the user lock below, is exactly the reversed
    # order this fix removes.
    probe = db.query(LibraryItem.user_id).filter(LibraryItem.id == task.item_id).first()
    if not probe:
        return True  # the item itself is gone — nothing left to reconcile
    owning_user_id = probe[0]

    user = (
        db.query(User).filter(User.id == owning_user_id)
        .populate_existing().with_for_update().first()
    )
    if not user:
        return True

    item = (
        db.query(LibraryItem).filter(LibraryItem.id == task.item_id)
        .populate_existing().with_for_update().first()
    )
    if not item:
        return True  # deleted between the probe and the lock
    if item.user_id != owning_user_id:
        # Ownership changed between the probe and the lock — should be
        # unreachable (user_id is immutable in practice) but treat as
        # moot rather than mutate under a since-stale owner.
        return True

    if item.reconciliation_generation != task.generation:
        # A newer fencing event has since superseded the generation this
        # task describes — a stale claimant must not touch the item under
        # a generation that is no longer current. The NEWER task (for the
        # CURRENT generation) is what matters now; this one is resolved
        # as moot, not retried forever.
        return True

    if item.entitlement_status != "released" or item.processed is not False:
        # Already reconciled by a concurrent pass (or the row never
        # actually carried the fenced signature this task assumes) —
        # nothing left to do.
        return True

    # Re-lock and re-check THIS task's own claim before mutating anything.
    # A stale Runner A whose claim TTL has since elapsed must not proceed
    # just because it got this far; only the runner that currently and
    # actually holds the claim may resolve it.
    fresh_task = (
        db.query(ReconciliationTask).filter(ReconciliationTask.id == task.id)
        .populate_existing().with_for_update().first()
    )
    if not fresh_task or fresh_task.claimed_by != runner_id:
        return False  # lost the claim — remain retryable, do not mutate
    if fresh_task.claimed_until is None or fresh_task.claimed_until < datetime.utcnow():
        return False  # our own claim already expired — remain retryable

    if user.effective_premium:
        item.entitlement_status = "premium"
    else:
        limit = free_source_limit()
        if user.successful_sources_total < limit:
            item.entitlement_status = "consumed"
            db.query(User).filter(User.id == user.id).update(
                {"successful_sources_total": User.successful_sources_total + 1},
                synchronize_session="fetch",
            )
        else:
            # No room left — grandfathered, exactly like a pre-cutover
            # excess source: stored, visible, selectable later, never
            # counted now.
            item.entitlement_status = "grandfathered"

    # Restores the correct terminal `processed` state ATOMICALLY with the
    # accounting decision above — same commit, same transaction, so an
    # observer can never see "accounted for" and "still marked
    # unprocessed" as two separate, out-of-sync moments.
    item.processed = True
    db.commit()
    return True


def retry_reconciliation_tasks(db: Session, batch_size: int = 25) -> tuple:
    """Scan `reconciliation_tasks` for 'pending'/'failed' rows not
    currently claimed by another live runner, resolve each via
    `_resolve_one_reconciliation_task`, using the EXACT same claim-lease
    pattern as `retry_cleanup_tasks` (claim under lock, act, release only
    this runner's own still-current claim). Returns `(resolved_count,
    failed_count)` — same never-collapse-to-one-number contract as every
    other reconciliation pass in this module."""
    from datetime import datetime as _dt, timedelta as _td
    from app.models.library import ReconciliationTask

    runner_id = str(uuid.uuid4())
    claim_ttl = _td(minutes=5)
    now = _dt.utcnow()

    candidate_ids = [
        tid for (tid,) in db.query(ReconciliationTask.id)
        .filter(
            ReconciliationTask.state.in_(("pending", "failed")),
            (ReconciliationTask.claimed_until.is_(None)) | (ReconciliationTask.claimed_until < now),
        )
        .order_by(ReconciliationTask.updated_at.asc())
        .limit(batch_size)
        .all()
    ]

    resolved, failed = 0, 0
    for task_id in candidate_ids:
        task = (
            db.query(ReconciliationTask).filter(ReconciliationTask.id == task_id)
            .populate_existing().with_for_update().first()
        )
        if not task or task.state not in ("pending", "failed"):
            continue  # resolved (or no longer eligible) since the scan above
        if task.claimed_until is not None and task.claimed_until >= _dt.utcnow():
            continue  # a concurrent runner already holds this exact row
        task.claimed_by = runner_id
        task.claimed_until = _dt.utcnow() + claim_ttl
        db.commit()

        ok = False
        try:
            ok = _resolve_one_reconciliation_task(db, task, runner_id)
        except Exception:
            db.rollback()
            ok = False
            logger.exception(
                "reconciliation task %s failed (item %s, generation %s)",
                task.id, task.item_id, task.generation,
            )

        fresh = (
            db.query(ReconciliationTask).filter(ReconciliationTask.id == task_id)
            .populate_existing().with_for_update().first()
        )
        if fresh and fresh.claimed_by == runner_id:
            fresh.state = "resolved" if ok else "failed"
            if not ok:
                fresh.retry_count = (fresh.retry_count or 0) + 1
            fresh.claimed_by = None
            fresh.claimed_until = None
            db.commit()

        if ok:
            resolved += 1
        else:
            failed += 1

    return resolved, failed


# ── Autonomous item-deletion retry (Task 2 closeout, Verified Blocker 6) ────
def retry_item_deletions(db: Session, batch_size: int = 25) -> tuple:
    """Discover and retry tombstoned `library_items` rows
    (`deletion_state IN ('pending', 'failed')`) whose external cleanup
    previously failed, using the EXACT durable identity `DELETE /library/
    {id}`'s PHASE 1 already wrote (`file_url`, `images`,
    `deletion_detail`) — no separate snapshot needed, since the row
    hasn't been hard-deleted yet.

    Runs on this repo's production scheduler, alongside cleanup-task and
    reconciliation-task retry — a user who closed the app right after a
    failed delete is no longer the only thing that can ever finish the
    job; this makes it happen automatically. Global lock order: user row
    FIRST, then item row, same as every other function in this module.

    Each item is independent: one item's failed artifact (or a genuinely
    unbuildable provider client) must never block an unrelated item's
    retry, so each is wrapped in its own try/except. Returns
    `(resolved_count, failed_count)` — same never-collapse-to-one-number
    contract as every other autonomous pass in this module."""
    from app.models.library import LibraryItem
    from app.routers import library as _library_router

    candidate_ids = [
        iid for (iid,) in db.query(LibraryItem.id)
        .filter(LibraryItem.deletion_state.in_(("pending", "failed")))
        .order_by(LibraryItem.updated_at.asc())
        .limit(batch_size)
        .all()
    ]

    resolved, failed = 0, 0
    for item_id in candidate_ids:
        # Determine the owning user with a PLAIN read first — never hold
        # the item lock while about to acquire the user lock (the exact
        # reversed-order hazard Verified Blocker 5 fixed for
        # reconciliation; the same global convention applies here).
        probe = db.query(LibraryItem.user_id).filter(LibraryItem.id == item_id).first()
        if not probe:
            continue  # already gone — finished by a concurrent runner
        owning_user_id = probe[0]

        user = (
            db.query(User).filter(User.id == owning_user_id)
            .populate_existing().with_for_update().first()
        )
        item = (
            db.query(LibraryItem).filter(LibraryItem.id == item_id)
            .populate_existing().with_for_update().first()
        )
        if not item or item.deletion_state not in ("pending", "failed"):
            continue  # resolved by a concurrent runner since the scan above
        if item.user_id != owning_user_id:
            continue  # ownership changed between the probe and the lock — moot
        # `user` may legitimately be None (e.g. a User row erasure already
        # removed) — the lock above is for lock-order consistency/
        # coordination with account erasure, not because this cleanup
        # writes anything onto the user row; proceed regardless so an
        # orphaned item is never permanently stuck.

        try:
            ok = _library_router._finish_item_deletion_cleanup(db, item, owning_user_id)
        except Exception:
            db.rollback()
            ok = False
            logger.exception("autonomous item-deletion retry failed for item %s", item_id)

        if ok:
            resolved += 1
        else:
            failed += 1

    return resolved, failed


# ── Grace-period promotion (Aug 2026) ────────────────────────────
def promote_scheduled_erasures(db: Session, batch_size: int = 25) -> int:
    """A 'scheduled' account erasure (see AccountErasure's docstring and
    `app/routers/auth.py`'s delete_account) sits inert for its grace
    period — account fully usable, nothing gated, no identity captured
    yet. Once that window elapses, THIS function captures the account's
    CURRENT identity (fresh, not whatever it looked like the moment the
    user first tapped delete — the account was usable that whole time,
    so a new upload or a deleted book since then must be accounted for)
    and flips the row to 'pending', handing off to the existing
    `retry_account_erasures` machinery for the actual cleanup — run
    immediately after this in the same maintenance-cycle tick, so a
    freshly-promoted row gets its first cleanup attempt right away
    rather than waiting a further 5 minutes.

    Audit finding (Aug 2026): `_capture_erasure_identity` calls RevenueCat's
    REST API (`_capture_erasure_snapshot` -> `_revenuecat_subscriber_detail`,
    a real `requests.get(..., timeout=10)`), so it must NEVER run while
    holding this row's lock — up to 10s of a live Postgres lock held across
    live network I/O, exactly the discipline `retry_account_erasures` above
    already avoids by committing its claim BEFORE calling its own slow
    cleanup function. Fixed to match: claim under lock and commit (releasing
    the lock) BEFORE the slow call, then re-acquire and re-verify the state
    is still 'scheduled' before writing the captured identity — a
    concurrent `POST /auth/cancel-deletion` (which doesn't respect
    claimed_until; it's user-facing, not a runner) can freely delete the
    row while identity capture is in flight, and this discards the
    now-orphaned result rather than resurrecting a cancelled row."""
    from app.models.library import AccountErasure
    from app.models.user import User
    from app.routers.auth import _capture_erasure_identity

    runner_id = str(uuid.uuid4())
    claim_ttl = timedelta(minutes=5)
    grace = timedelta(hours=settings.account_deletion_grace_hours)
    cutoff = datetime.utcnow() - grace

    candidate_ids = [
        eid for (eid,) in db.query(AccountErasure.id)
        .filter(
            AccountErasure.state == "scheduled",
            AccountErasure.requested_at <= cutoff,
            (AccountErasure.claimed_until.is_(None)) | (AccountErasure.claimed_until < datetime.utcnow()),
        )
        .order_by(AccountErasure.requested_at.asc())
        .limit(batch_size)
        .all()
    ]

    promoted = 0
    for erasure_id in candidate_ids:
        erasure = (
            db.query(AccountErasure).filter(AccountErasure.id == erasure_id)
            .populate_existing().with_for_update().first()
        )
        if not erasure or erasure.state != "scheduled":
            continue  # cancelled (or already promoted by a concurrent runner) since the scan above
        if erasure.claimed_until is not None and erasure.claimed_until >= datetime.utcnow():
            continue  # a concurrent runner already holds this exact row

        user = db.query(User).filter(User.id == erasure.user_id).first()
        if user is None:
            # Account row is gone some other way — nothing left to erase.
            db.delete(erasure)
            db.commit()
            continue

        # Claim and release the lock BEFORE the slow network call.
        erasure.claimed_by = runner_id
        erasure.claimed_until = datetime.utcnow() + claim_ttl
        db.commit()

        identity = _capture_erasure_identity(db, user)

        # Re-acquire and re-verify: a cancel could have deleted this row
        # entirely while identity capture was in flight above.
        erasure = (
            db.query(AccountErasure).filter(AccountErasure.id == erasure_id)
            .populate_existing().with_for_update().first()
        )
        if not erasure or erasure.state != "scheduled":
            continue  # cancelled while capturing identity — discard the result

        erasure.identity = identity
        erasure.state = "pending"
        erasure.claimed_by = None
        erasure.claimed_until = None
        db.commit()
        promoted += 1

    return promoted


# ── Autonomous account-erasure retry (Task 2 closeout, Verified Blocker 8) ──
def retry_account_erasures(db: Session, batch_size: int = 25) -> tuple:
    """Discover and retry `account_erasures` rows in 'pending'/'failed'
    state, using the SAME claim-lease protocol `retry_cleanup_tasks`/
    `retry_reconciliation_tasks` already use — each candidate row is
    individually claimed (`claimed_by`/`claimed_until`) under a row lock
    before work starts, so two concurrent runners retry disjoint rows
    rather than double-processing one.

    Delegates the actual per-artifact attempt to `app.routers.auth.
    _attempt_account_erasure_cleanup` — the SAME function `DELETE /auth/
    me` calls — so a user re-tapping delete and this autonomous pass can
    never drift onto two different implementations of "what does erasure
    actually do". Runs on this repo's production scheduler alongside
    cleanup-task, reconciliation-task and item-deletion retry."""
    from datetime import datetime as _dt, timedelta as _td
    from app.models.library import AccountErasure
    from app.routers import auth as _auth_router

    runner_id = str(uuid.uuid4())
    claim_ttl = _td(minutes=5)
    now = _dt.utcnow()

    candidate_ids = [
        eid for (eid,) in db.query(AccountErasure.id)
        .filter(
            AccountErasure.state.in_(("pending", "failed")),
            (AccountErasure.claimed_until.is_(None)) | (AccountErasure.claimed_until < now),
        )
        .order_by(AccountErasure.updated_at.asc())
        .limit(batch_size)
        .all()
    ]

    resolved, failed = 0, 0
    for erasure_id in candidate_ids:
        erasure = (
            db.query(AccountErasure).filter(AccountErasure.id == erasure_id)
            .populate_existing().with_for_update().first()
        )
        if not erasure or erasure.state not in ("pending", "failed"):
            continue  # resolved (or no longer eligible) since the scan above
        if erasure.claimed_until is not None and erasure.claimed_until >= _dt.utcnow():
            continue  # a concurrent runner already holds this exact row
        erasure.claimed_by = runner_id
        erasure.claimed_until = _dt.utcnow() + claim_ttl
        db.commit()

        try:
            ok = _auth_router._attempt_account_erasure_cleanup(db, erasure)
        except Exception:
            db.rollback()
            ok = False
            logger.exception("autonomous account-erasure retry failed for erasure %s", erasure_id)

        if ok:
            # On full success `_attempt_account_erasure_cleanup` already
            # deleted the row (and therefore its claim) in the same
            # commit — nothing left here to release.
            resolved += 1
            continue

        # Release ONLY this runner's own claim lease, guarded by
        # claimed_by == runner_id — if a newer runner has since reclaimed
        # this row, clearing its claim here would release THEIR lease
        # early, letting a third runner double-claim a row the second
        # may still be actively working.
        fresh = (
            db.query(AccountErasure).filter(AccountErasure.id == erasure_id)
            .populate_existing().with_for_update().first()
        )
        if fresh and fresh.claimed_by == runner_id:
            fresh.claimed_by = None
            fresh.claimed_until = None
            db.commit()
        failed += 1

    return resolved, failed
