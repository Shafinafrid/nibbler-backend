from sqlalchemy import Column, String, ForeignKey, DateTime, Boolean, Integer, Text, JSON, func, UniqueConstraint
from sqlalchemy.orm import relationship
from app.database import Base


class LibraryItem(Base):
    __tablename__ = "library_items"

    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    # Figures extracted from the source at upload — see services/image_extract.
    # [{ref, page, w, h, context}]; empty for books with no usable pictures.
    images = Column(JSON, nullable=True)
    # Scanned PDFs: 'needed' once extraction finds no text, then 'running' /
    # 'done' / 'failed'. Null for every book with a real text layer.
    ocr_status = Column(String, nullable=True)
    ocr_pages_done = Column(Integer, default=0)
    ocr_pages_total = Column(Integer, default=0)
    type = Column(String, nullable=False)       # pdf | url | text | note
    content = Column(Text, nullable=True)         # raw text or pasted content
    file_url = Column(String, nullable=True)      # S3 object key (pre-July-2026 rows hold full public URLs)
    file_size = Column(Integer, nullable=True)    # bytes
    source_url = Column(String, nullable=True)    # original URL for scraped articles
    processed = Column(Boolean, default=False)
    chunk_count = Column(Integer, default=0)      # number of Pinecone vectors
    processing_error = Column(String, nullable=True)  # error message if processing failed
    # Whether the ORIGINAL uploaded file actually reached S3. `processed` only
    # ever meant "text extracted + indexing attempted", but it was being read
    # as if it also implied the file was archived — so a silent S3 failure left
    # a row that looked healthy with no original behind it.
    #   None    → pre-2026-07-26 row, unknown
    #   stored  → the original is in S3 at file_url
    #   failed  → archival failed; file_url is NULL and nothing retried
    archive_status = Column(String, nullable=True)
    # ── Nibble-session fields (July 2026) ──
    mode = Column(String, default="wisdom")            # wisdom | story
    kind = Column(String, default="book")              # book | article | paper
    author = Column(String, nullable=True)
    growth_profile_name = Column(String, nullable=True)  # premium: which profile this feeds
    story_progress = Column(Integer, default=0)          # story mode: next chunk index to read
    is_active = Column(Boolean, default=True)            # feeds nibble generation (≤5 active per user)
    # Free-tier lock selection (Task 2, Aug 2026): true if this item is one of
    # the persisted ≤3 sources chosen (explicitly, or by deterministic
    # fallback) to stay usable for a Free account. Dormant while the account
    # is Premium/trial/complimentary — see is_source_unlocked() in
    # app/services/entitlement_service.py, the ONLY place that should read
    # this column to decide access; never compare it directly elsewhere.
    is_unlocked_selection = Column(Boolean, default=False, nullable=False)
    # Provenance of the CURRENT selection (only meaningful while
    # is_unlocked_selection is True): 'explicit' (the user picked it via
    # PUT /library/free-selection), 'fallback' (computed deterministically
    # at a real downgrade), 'legacy_fallback' (assigned by the pre-cutover
    # migration backfill — see entitlement_service._backfill_one). Lets the
    # app be honest about "you chose this" vs "we picked this for you", and
    # stops a migration-era provisional pick from ever being mistaken for a
    # real user choice months later.
    selection_kind = Column(String, nullable=True)
    # The authoritative "last genuinely used" signal for the deterministic
    # downgrade fallback (most-recently-active wins) — deliberately NOT
    # `updated_at`, which also changes on a bare rename or growth-profile
    # edit that has nothing to do with actual use. Updated on real
    # activation/use events: item creation, PATCH .../active turning ON, and
    # a session actually being generated for it.
    last_active_at = Column(DateTime, nullable=True)
    # ── Free-tier lifetime-entitlement reservation (Task 2 remediation, Aug
    # 2026) — see entitlement_service.reserve_free_capacity/
    # finalize_successful_processing. One of:
    #   None        → never reserved (not yet processed, or pre-Task-2-
    #                 remediation row not yet touched by the migration)
    #   'pending'   → a Free reservation is held; processing is in flight
    #   'consumed'  → processing succeeded; PERMANENTLY counted in the
    #                 owning user's successful_sources_total
    #   'premium'   → created while entitled; never counts against the Free
    #                 lifetime limit regardless of the account's CURRENT tier
    #   'released'  → a reservation was returned (processing failed, or the
    #                 reservation expired uncompleted) — does not count
    #   'rejected'  → capacity was full at reservation time; never processed
    #   'grandfathered' → pre-cutover row the migration could not attribute
    #                 to a real reservation (see _backfill_one) — stored,
    #                 selectable, never counted
    entitlement_status = Column(String, nullable=True)
    # When a 'pending' reservation was made — kept as audit metadata (first-
    # reserved time). Reaping itself now goes by `reservation_lease_expires_at`
    # below, not this — see the renewable-lease remediation.
    reserved_at = Column(DateTime, nullable=True)
    # ── Renewable reservation lease (Task 2 remediation #3, Aug 2026) ──────
    # A fixed 30-minute reserved_at cutoff could reap a genuinely active OCR/
    # indexing worker on a slow book. This pair makes "abandoned" a property
    # of a LEASE that live work renews, not of a clock that started ticking
    # once and never listens again.
    #   reservation_lease_token   → random id minted fresh every time this
    #     item transitions into 'pending' (a first reservation, or a retry
    #     after a reap). A worker holding an OLDER token is provably talking
    #     about a SUPERSEDED attempt — reserve_free_capacity/
    #     renew_reservation_lease/finalize_successful_processing all refuse
    #     to act for a caller whose token no longer matches what's stored,
    #     which is what stops a stale worker from double-converting a
    #     reservation a newer attempt already holds.
    #   reservation_lease_expires_at → extended by renew_reservation_lease()
    #     on every real progress checkpoint (e.g. each OCR page-progress
    #     callback). Only a lease that nothing has renewed past this moment
    #     is reapable — active work extends its own deadline instead of
    #     racing a fixed one.
    reservation_lease_token = Column(String, nullable=True)
    reservation_lease_expires_at = Column(DateTime, nullable=True)
    # ── Attempt-scoped ownership (Task 2, 3rd-audit remediation #1/#4) ─────
    # A durable "which attempt currently owns this item's processing" marker
    # — minted alongside reservation_lease_token but, unlike that field,
    # NEVER cleared by finalize/release. reservation_lease_token/expires_at
    # answer "is a Free capacity reservation currently active"; this answers
    # "which attempt (Free OR Premium-created) most recently claimed this
    # item", which every pipeline (not just OCR) now needs even after the
    # reservation itself has been finalized/released — because Pinecone
    # vector ids and this item's fixed S3 archive key are BOTH overwritten
    # in place by a retry, so a stale attempt's compensating cleanup must be
    # able to prove it is still the current owner before deleting anything,
    # or it can delete a newer attempt's artifacts out from under it.
    last_processing_attempt_id = Column(String, nullable=True)
    # ── Worker-attempt admission (Task 2 final consolidated backend pass) ──
    # A SEPARATE concept from `reservation_lease_token` above: that field
    # answers "does this account still hold one of its 3 permanent Free
    # slots reserved" (a capacity question, Free-only, may legitimately
    # persist across an extraction attempt that finds no text AND a later
    # OCR attempt on the SAME item). This pair answers "is there a LIVE
    # worker invocation currently processing this item right now" (an
    # admission question, every tier, one fresh immutable id per actual
    # worker invocation — extraction and the later OCR continuation get
    # TWO DIFFERENT worker_attempt_id values while sharing ONE capacity
    # reservation). `admit_worker_attempt`/`renew_worker_attempt`/
    # `release_worker_attempt` (entitlement_service.py) are the only
    # functions that touch this pair. A second caller finding a live
    # (unexpired) `worker_attempt_id` is rejected before any external
    # action and never receives this value — see `admit_worker_attempt`.
    worker_attempt_id = Column(String, nullable=True)
    worker_attempt_expires_at = Column(DateTime, nullable=True)
    # ── Mixed-version cutover fencing generation (Task 2 consolidated pass) ─
    # Stamped by the database fencing trigger (app/database.py,
    # _ensure_mixed_version_fencing) the instant it fences an old/tokenless
    # worker's `processed=True` write to `processed=False`/
    # `entitlement_status='released'`. Paired with the SAME value on the
    # matching `ReconciliationTask` row so the autonomous reconciliation
    # worker can prove it is resolving the fencing event it thinks it is —
    # a stale claimant whose task's generation no longer matches this
    # column (a newer fencing event has since superseded it) must not
    # mutate the item.
    reconciliation_generation = Column(String, nullable=True)
    # ── Durable cleanup-needed marker (Task 2, 3rd-audit remediation #4) ───
    # Set when a terminal-failure compensating cleanup is ATTEMPTED, cleared
    # when it succeeds. If cleanup itself raises (Pinecone/S3 unreachable),
    # this persists as a durable, retryable record — "log and forget" left
    # no way to ever find and finish an interrupted cleanup again.
    #   None      → nothing pending
    #   'pending' → cleanup was scheduled for this attempt, not yet resolved
    #   'failed'  → cleanup was attempted and raised; needs a retry
    cleanup_state = Column(String, nullable=True)
    # JSON: {"attempt_token": str, "vectors": bool, "s3_key": str|None,
    #        "reason": str} — everything a future retry needs to finish the
    # job without re-deriving what this attempt actually created.
    cleanup_detail = Column(JSON, nullable=True)
    # ── Durable deletion state machine (Task 2, 3rd-audit remediation #9) ──
    # DELETE /library/{id} is now two-phase: a fast, durable "deletion
    # accepted" commit FIRST (this pair), external cleanup SECOND, and only
    # on cleanup's full success does the row actually get hard-deleted. A
    # cleanup failure therefore can never disappear into a log line after
    # the row is already gone — the row survives, tombstoned, with exactly
    # the identity (`deletion_detail`) a retry needs, and every retry (the
    # user re-tapping delete, or a future ops retry) picks up from here
    # instead of re-deriving what to clean from a row that may no longer
    # have the answer.
    #   None      → not being deleted
    #   'pending' → deletion accepted, cleanup not yet confirmed complete
    #   'failed'  → cleanup was attempted at least once and did not fully
    #               succeed; retryable
    deletion_state = Column(String, nullable=True)
    # JSON: {"s3_key": str|None, "image_keys": [str]} — captured at the
    # moment deletion is accepted, so cleanup always knows what THIS item
    # owned even if some other field is later cleared.
    deletion_detail = Column(JSON, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    user = relationship("User", back_populates="library_items")


class CleanupTask(Base):
    """Durable, attempt-scoped compensating-cleanup ledger (Task 2 lifecycle
    remediation, Aug 2026, Follow-up 2A). Replaces the old single-slot
    `LibraryItem.cleanup_state`/`cleanup_detail` marker, which could only
    ever describe the row's CURRENT owner — a superseded attempt's cleanup
    failure had nowhere durable to live once a newer attempt took over the
    row. One row here identifies a SPECIFIC (item, user, attempt, artifact
    kind) unit of cleanup work independent of whoever currently owns the
    `library_items` row, so:
      - a stale attempt's failed cleanup remains discoverable and retryable
        even after a newer attempt has taken over the item;
      - one attempt can have INDEPENDENT vector and S3 cleanup records
        (different artifact kinds), each resolved on its own;
      - a newer attempt's own row/artifacts are never relabelled as an
        older attempt's cleanup, because this table never touches
        `library_items` at all — everything needed to identify and retry
        the work lives on THIS row.

    Column names are deliberately the exact vocabulary this codebase
    already uses for attempt/artifact identity and cleanup state
    (`item_id`, `user_id`, `attempt_token`, `artifact_kind`,
    `cleanup_state`, `artifact_key`, `reason`) — not a new naming scheme —
    since retry/discovery tooling is expected to recognize durable cleanup
    work by FIELD SHAPE, not by this table's name.

    `reason`/`last_error` never hold source text, embeddings, secrets, or
    credentials — only a short, human-readable failure description.
    """
    __tablename__ = "cleanup_tasks"
    __table_args__ = (
        # Task 2 closeout (Verified Blocker 4): identity now includes
        # `artifact_key`, not just `artifact_kind` — the old 3-column
        # constraint let AT MOST ONE 's3'/'vectors' record exist per
        # attempt, which made per-image cleanup (many keys, one attempt)
        # impossible: a second image's cleanup-pending upsert would just
        # overwrite the first's `artifact_key` in place, silently losing
        # its identity. `artifact_key` is NEVER stored as NULL (kinds with
        # no natural key, like 'vectors', use the empty string "" as their
        # normalized key) specifically so this constraint's uniqueness is
        # real — Postgres treats every NULL as distinct, which would have
        # let unlimited duplicate 'vectors' rows through silently.
        UniqueConstraint(
            "item_id", "attempt_token", "artifact_kind", "artifact_key",
            name="uq_cleanup_task_identity_v2",
        ),
    )

    id = Column(String, primary_key=True)
    item_id = Column(String, index=True, nullable=False)
    user_id = Column(String, index=True, nullable=False)
    attempt_token = Column(String, index=True, nullable=False)
    artifact_kind = Column(String, nullable=False)   # 'vectors' | 's3' | 's3_image'
    # Exact provider key for 's3'/'s3_image'; the normalized empty string
    # "" (never NULL — see __table_args__ above) for 'vectors', which has
    # no per-artifact key of its own.
    artifact_key = Column(String, nullable=False, default="")
    cleanup_state = Column(String, nullable=False, default="pending")  # 'pending' | 'failed' | 'resolved'
    reason = Column(String, nullable=True)            # short failure detail, never source/secrets
    retry_count = Column(Integer, nullable=False, default=0)
    # Claim lease for the autonomous runner (requirement 8: "claim work
    # safely against concurrent runners") — a runner claims a row by
    # stamping its own run id + a near-future lease here, under a row lock,
    # so a second concurrent runner skips rows another runner is already
    # working on instead of double-processing them.
    claimed_by = Column(String, nullable=True)
    claimed_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ReconciliationTask(Base):
    """Durable mixed-version-cutover reconciliation queue (Task 2
    consolidated backend pass). An old/tokenless worker can still finish
    AFTER the fencing trigger (app/database.py, _ensure_mixed_version_
    fencing) has already reacted to its `processed=True` write — the
    trigger fences the row (`processed=False`, `entitlement_status=
    'released'`, a fresh `reconciliation_generation` stamped on the item)
    and, in the SAME transaction, inserts/upserts exactly one row here
    naming that generation. `retry_reconciliation_tasks` (entitlement_
    service.py) is the autonomous worker — wired onto the same production
    scheduler as cleanup retry — that discovers pending rows and restores
    the item's correct terminal state (processed=True + the right
    entitlement_status) atomically with lifetime-capacity accounting.

    One row per item (`item_id` unique) rather than one per fencing event:
    an item can only be fenced once in practice (entitlement_status never
    reverts to NULL once accounted), but if it somehow were fenced again,
    the trigger's upsert stamps the NEWEST generation here and the worker
    only ever acts if the item's CURRENT generation still matches the
    task's — a stale claimant can never resolve a superseded generation.
    """
    __tablename__ = "reconciliation_tasks"
    __table_args__ = (
        UniqueConstraint("item_id", name="uq_reconciliation_task_item"),
    )

    id = Column(String, primary_key=True)
    item_id = Column(String, index=True, nullable=False)
    # The exact fencing-event identity this task describes — must match
    # LibraryItem.reconciliation_generation at the moment the worker acts,
    # or the task is stale and must not mutate the item.
    generation = Column(String, nullable=False)
    state = Column(String, nullable=False, default="pending")  # 'pending' | 'failed' | 'resolved'
    retry_count = Column(Integer, nullable=False, default=0)
    claimed_by = Column(String, nullable=True)
    claimed_until = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class AccountErasure(Base):
    """Durable GDPR-erasure state machine (Task 2 closeout, Verified
    Blocker 8). Replaces the old `DELETE /auth/me`, which deleted the
    Postgres `User` row unconditionally even when S3/Pinecone/Firebase
    cleanup failed — losing both the retry identity (nothing on the row
    anymore names what still needs cleaning) and the ability to fail
    closed (a User row that no longer exists cannot gate anything).

    One row per user (`user_id` unique). Created BEFORE any external
    deletion is attempted, holding the COMPLETE durable identity erasure
    needs — every current source-file key, every image key, the avatar
    key, the Pinecone namespace, every still-unresolved `cleanup_tasks`
    row this user owns, and the Firebase uid — so cleanup can proceed
    correctly even if something else changes or fails partway through.
    The presence of a 'pending'/'failed' row here is also the fail-closed
    gate `get_current_user` checks on every authenticated request for
    this account (see app/middleware/auth.py).

    `identity` never holds credentials, source text, embeddings, or
    tokens — only the artifact identities (ids/keys) needed to find and
    delete them."""
    __tablename__ = "account_erasures"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_account_erasure_user"),
    )

    id = Column(String, primary_key=True)
    user_id = Column(String, index=True, nullable=False)
    state = Column(String, nullable=False, default="pending")  # 'pending' | 'failed' | 'resolved'
    # {"source_keys": [...], "image_keys": [...], "avatar_key": str|None,
    #  "pinecone_namespace": str, "cleanup_ledger_ids": [...],
    #  "firebase_uid": str}
    identity = Column(JSON, nullable=False)
    # Per-artifact-class outcome as of the last attempt — {"vectors": bool,
    # "s3_files": bool, "s3_images": bool, "avatar": bool,
    # "ledger_artifacts": bool, "firebase": bool}. None/missing means "not
    # yet attempted".
    progress = Column(JSON, nullable=True)
    claimed_by = Column(String, nullable=True)
    claimed_until = Column(DateTime, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    requested_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # ── Google Sheet operational-log mirror (Task 7, Aug 2026) ──────────────
    # Row NUMBERS on the "Deletion Log" / "User Snapshot" tabs this job's row
    # lives at, captured once on first successful write so later syncs UPDATE
    # the same row instead of appending duplicates. Postgres stays the
    # source of truth throughout — the Sheet is a best-effort mirror, never
    # read back to make a decision.
    sheet_row = Column(Integer, nullable=True)
    snapshot_sheet_row = Column(Integer, nullable=True)
    deletion_started_at = Column(DateTime, nullable=True)
    deletion_completed_at = Column(DateTime, nullable=True)
    personal_data_redacted_at = Column(DateTime, nullable=True)
