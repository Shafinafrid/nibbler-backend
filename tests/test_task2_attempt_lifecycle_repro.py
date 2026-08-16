"""
Task 2 — ingestion-attempt lifecycle acceptance harness (final, through
Follow-up 2B-R1). Implementation-neutral A–Z + MIXED-1..4 sections proving
immutable attempt ownership, full-attempt heartbeat coverage, attempt-
scoped external artifacts, durable compensating cleanup, autonomous cleanup
retry, and mixed-version (old-reader/new-trigger) rollout fencing — driving
the REAL current implementation throughout (imported from app/, never
copied or reimplemented). This file makes NO production-code changes.

SCOPE (sections, one line each — see each section's own inline docstring/
comments for full design rationale):
  A/F  — Pinecone/S3 false-return (non-raising) cleanup failure.
  B    — Stale-attempt cleanup identity loss (queries the implementation-
         neutral find_cleanup_record() adapter, never the item row directly).
  C    — Stale-worker error mutation, via a token-aware invocation adapter.
  D    — Pre-first-batch heartbeat coverage gap (text + PDF), via polling
         for a REAL production heartbeat renewal before invoking the reaper
         — never monkeypatched, never timing-based (see the deterministic-
         supersession barrier below for the STALE variant of this idea).
  E    — PG14 harness CAPABILITY check (scan-vs-lock barrier placement) —
         not a product invariant; check_capability()/exit 2 on failure.
  G    — Superseded-attempt archive-key race; both attempts driven through
         the real archive path, keys observed via find_artifact_key(),
         never constructed — accepts both fixed-key and attempt-scoped-key
         production designs as valid GREEN outcomes.
  H1/H2/H3 (live) — full-attempt heartbeat coverage for EPUB/URL/OCR.
  H2-STALE/H3-STALE/I1/L/O/P1/Q — GENUINE supersession via a deterministic
         barrier (`_deterministic_stale_supersession`/`_deterministic_
         heartbeat_gate`): the attempt guard's OWN background-thread
         renewal is made a provable no-op for the exact window under test
         (never relying on real elapsed time beating a ~2s heartbeat
         interval), then force-stale + the REAL reaper + a REAL re-
         reservation run with a certain, not merely likely, outcome.
  I2   — Autonomous cleanup-retry runner, discovered by semantic capability
         (references cleanup_state/cleanup_detail, excluding known item-
         specific write helpers) — never a guessed symbol name.
  J    — Mixed-version old-writer completion landing after the real,
         startup-only reconciliation sweep already ran once.
  K    — Premium attempt ownership: no fabricated entitlement-status reset
         — a narrowly-modeled reassignment of ONLY `last_processing_
         attempt_id` (the exact atomic ownership boundary under test),
         since no real production path currently supersedes a live Premium
         attempt.
  M    — Stale finalization after a newer attempt has already finalized —
         both a full real end-to-end worker (for B) and direct
         finalization-path coverage (for A's stale call), isolating the
         primitive exactly as production calls it.
  N    — Duplicate-worker admission, deterministically ordered (never a
         real-time two-thread race) so the second caller is GUARANTEED,
         not merely likely, to observe the first's already-committed
         reservation.
  O    — Post-upload row-mutation fence: accepts EITHER a safe early abort
         (before ever reaching row mutation/extraction) OR authoritative-
         validation-before-continuing as GREEN; A's real key is discovered
         from the real upload-call log, never assumed/constructed; row
         state is captured as an immutable scalar snapshot at the
         observation point, never a mutable ORM object read later.
  P1/P2 — OCR progress-write-before-check, and a REAL `_run_ocr` worker
         driven through its own real `finalize_successful_processing` call
         (gated by a deterministic barrier placed immediately around that
         real call) so whatever it does on rejection is observed, not
         reproduced by the test.
  R    — Background-heartbeat-ONLY database errors (raises only for calls
         from the guard's own background thread; the worker's own
         synchronous on_batch checkpoint reaches the REAL primitive
         unmodified) — may legitimately report GREEN, since the worker's
         own correct real-time checkpoint is a genuinely independent
         protection from the background thread's separate error-handling
         defect (see Section L for that defect's own direct proof).
  S    — Guard termination: accepts EITHER outcome — Section S's own EXACT
         guard thread (matched by name, never a process-global scan) is
         stopped and joined before the worker reports completion, OR the
         worker surfaces a specific lifecycle-shutdown failure.
  T    — Ledger-persistence failure, injected at the actual transaction/
         commit boundary (wraps SessionLocal so the first real `.commit()`
         call fails) — implementation-neutral (ORM, Core, or any other
         durable-ledger shape), covers both vectors and S3.
  U    — Cleanup survives live-row deletion; invoked via a signature-aware
         adapter (inspects the real callable's parameters) so a safe
         implementation that adds e.g. `user_id` becomes GREEN unedited.
  V    — A REAL two-session concurrent first-ledger-insert race against a
         disposable local PostgreSQL cluster (barrier-synchronized, real
         MVCC/row-locking) — not a sequential SQLite-based substitute; an
         earlier SQLite-only version was found genuinely non-deterministic
         under SQLite's coarse whole-database write lock and was replaced.
  W    — Autonomous cleanup retry: ACTUALLY EXECUTES every job currently
         registered on the real, live notification_service scheduler
         (never a direct `retry_cleanup_tasks()` call) and proves, by real
         execution, that none of them touch a durable failed cleanup task.
  X    — Claim-token-bound runner completion: Runner B is driven to a
         DISTINGUISHABLE authoritative result (a genuine, real provider
         failure, not a same-outcome success both runners would share) so
         a later claim-holder's clobber is unambiguously provable.
  Y1/Y2/Y3 — Malformed cleanup tasks (unknown kind, missing key) and a
         successful S3 retry's full row/ledger/claim consistency, with the
         target and control tasks deterministically ordered via distinct
         `updated_at` values (never insertion-order timing luck).
  Z    — Post-finalization image ownership: both `extract_and_store` and
         the real `image_extract.delete_stored` boundary are hermetically
         mocked (no real S3Service is ever instantiated); accepts EITHER
         genuine object removal OR a durable, reflectively-discoverable
         cleanup record as GREEN.
  MIXED-1 — Old-reader fencing: tests ONLY a mechanism this harness can
         actually execute (a real, direct old-reader query against current
         database state) — never claims to accept an unmodeled, unexecuted
         "external deployment drain."
  MIXED-2 — SQLite trigger replacement: `CREATE TRIGGER IF NOT EXISTS`
         preserving a stale installed definition under a name collision.
  MIXED-3 — SQLite corrective-UPDATE guard: a STATIC SQL-shape assertion
         only (the claimed cross-write interleaving was determined, by
         direct analysis of SQLite's synchronous-trigger and coarse-
         writer-lock semantics, to be UNREACHABLE — an earlier version
         fabricated a substitute by manually running the trigger's own
         UPDATE statement and calling that "the interleaving"; removed).
  MIXED-4 — A REAL disposable local PostgreSQL cluster: production-
         compatible schema (`Base.metadata.create_all`), the REAL
         `reconcile_unaccounted_processed_items` sweep genuinely executed
         (never a comment/no-op) before a real old-worker write, a REAL
         old-reader query (never hard-coded), and trigger-installation
         idempotency.

DETERMINISM: every barrier is an explicit Event/Barrier/mock-controlled
gate — no section relies on real elapsed time beating a background
interval. `_deterministic_stale_supersession`/`_deterministic_heartbeat_
gate` make the attempt guard's background-thread renewal a provable
no-op (or single-tick-then-frozen) for the exact window under test, so
force-stale-then-reap sequences have a certain outcome on every run.

EXTERNAL RESOURCES: this harness uses TWO kinds of real, disposable state:
  - SQLite (the default, for the great majority of sections) — a temp
    directory removed on every exit path via `_owned_tempdir`, including
    import/setup failures.
  - A REAL, disposable LOCAL PostgreSQL 16 cluster (`_disposable_postgres_
    cluster`, sections V and MIXED-4 only) — bootstrapped and torn down
    entirely within its own owned temp directory; teardown always attempts
    shutdown, VERIFIES it via `pg_ctl status`, disposes every engine this
    function itself opened, and reports (never swallows) a teardown
    failure as a harness/capability problem.
Zero calls to any live external network/provider service of any kind, in
either case — confirmed via `tests/hermetic.py`'s socket guard and this
file's own before/after residue diff (temp dirs by known prefix, live
thread names, live PostgreSQL-related process ids).

RESULT MODEL: each `check(...)` call states the DESIRED production
invariant directly.
  [PASS]         — the desired invariant holds right now.
  [FAIL]         — the desired invariant is violated right now (this is
                   what today's known-buggy code produces; the same call
                   becomes a PASS once the underlying production code is
                   fixed, with NO edit to this file).
  [SETUP ERROR]  — a harness/import/environment/thread/capability problem,
                   not a product-invariant result.
`check_capability(...)` is used for genuine test-tooling capability checks
(Section E; the real-PostgreSQL environment checks in V/MIXED-4) — a
capability failure is a harness problem (exit 2), never counted as a
failed product invariant (exit 1).

EXIT CODE (proven at the REAL OS-process level by `_prove_real_process_
exit_boundary`, launching this exact file as three separate subprocesses
via `NIBBLER_R1_SELFTEST_EXIT_MODE=green|red|setup`, not merely by the
pure `_decide_exit` self-check on fabricated inputs):
  2 — any setup/import/thread/capability/environment/teardown failure
      occurred (harness could not faithfully evaluate the invariants). A
      blocked outbound network attempt is classified as exit 2 by this
      file's OWN internal logic — though `tests/hermetic.py`'s own
      `atexit` handler (outside this assignment's authorized scope to
      modify) separately forces the FINAL OS-level exit code to 1 in that
      specific case via an unconditional `os._exit(1)`; see the
      completion report for this disclosed, unavoidable limitation.
  1 — otherwise, if any product invariant FAILed (this is what running
      this file against today's code is expected to produce).
  0 — otherwise (every invariant PASSed — the GREEN state after a fix).

Run: .venv311/bin/python tests/test_task2_attempt_lifecycle_repro.py
"""
import os
import sys
import time
import shutil
import inspect
import tempfile
import threading
import datetime
import traceback
import contextlib
import subprocess
import unittest.mock as mock

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

# Bounded deadline Section D polls for evidence of a production heartbeat
# renewal before invoking the reaper — long enough for a reasonable test
# heartbeat interval, short enough to keep the whole script practical.
# Current production has no full-attempt heartbeat/configuration hook at
# all for this phase, so this bound is never reached by anything renewing
# the lease; it only bounds how long the poll waits before concluding "no
# heartbeat observed", which is the correct, current-code outcome.
_HEARTBEAT_POLL_DEADLINE_SECONDS = 3.0
_HEARTBEAT_POLL_INTERVAL_SECONDS = 0.05


# ── Cleanup-ownership helper (Step 2) ────────────────────────────────────
@contextlib.contextmanager
def _owned_tempdir(prefix):
    """Creates a tempdir and GUARANTEES its removal on the way out — normal
    return, a raised exception, or anything in between. This is the single
    mechanism that owns `_TMP`-equivalent cleanup for the whole script; no
    other code path in this file calls shutil.rmtree() directly."""
    path = tempfile.mkdtemp(prefix=prefix)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _cleanup_self_check():
    """Exercises _owned_tempdir's guarantee directly: creates a nested,
    disposable directory, forces an exception through the context, and
    confirms the directory no longer exists afterward. Proves the SAME
    mechanism protecting this run's real temp state actually cleans up on
    an exception path — without recursively launching a full copy of this
    script. Returns (ok: bool, path: str)."""
    probe = {}
    try:
        with _owned_tempdir("nibbler-cleanup-selfcheck-") as p:
            probe["path"] = p
            if not os.path.isdir(p):
                raise AssertionError("tempdir was not created")
            raise RuntimeError("deliberate failure to prove cleanup-on-exception")
    except RuntimeError:
        pass
    survived = os.path.isdir(probe.get("path", ""))
    return (not survived), probe.get("path")


def _decide_exit(results_fail, harness_errors):
    """Pure exit-code decision, isolated so it can be proven correct on
    synthetic inputs independent of any real section outcome (see
    `_exit_semantics_self_check`)."""
    if harness_errors:
        return 2
    if results_fail:
        return 1
    return 0


def _exit_semantics_self_check():
    """Proves the RED/GREEN/SETUP exit contract with fabricated inputs —
    does not touch the DB, imports, or any real section result. This is
    the concrete, executable proof that: all invariants green -> 0; any
    failed invariant (with no harness error) -> 1; any harness/capability
    error -> 2, regardless of what else failed."""
    ok = (
        _decide_exit([], []) == 0
        and _decide_exit(["some failed invariant"], []) == 1
        and _decide_exit([], ["some harness error"]) == 2
        and _decide_exit(["some failed invariant"], ["some harness error"]) == 2
    )
    return ok


# ── Semantic cleanup-record adapter (Follow-up 1C) ───────────────────────
# `_cleanup_record_matches` is the pure predicate at the heart of
# `find_cleanup_record` (defined inside run_all(), where the DB is
# available). Keeping it pure and module-level lets it be self-checked
# against fabricated in-memory records (see `_cleanup_adapter_self_check`)
# using the EXACT SAME matching logic the real Section B/G queries use —
# not a parallel reimplementation of the matching rules.
def _cleanup_record_matches(record, item_id, user_id, attempt_token, artifact_kind, artifact_key=None):
    """A normalized cleanup record (see module docstring for the shape)
    matches a query when: it is for the exact item/user/attempt/kind, and
    — for kind=='s3' only — it carries a non-null artifact_key that equals
    the queried key whenever one was given. kind=='vectors' never requires
    an artifact_key, matching current production's combined per-attempt
    marker (vector cleanup is attempted unconditionally on every
    compensation call, regardless of whether an s3_key is also present)."""
    if record is None:
        return False
    if record.get("item_id") != item_id or record.get("user_id") != user_id:
        return False
    if record.get("attempt_token") != attempt_token:
        return False
    if record.get("artifact_kind") != artifact_kind:
        return False
    if artifact_kind == "s3":
        rec_key = record.get("artifact_key")
        if rec_key is None:
            return False
        if artifact_key is not None and rec_key != artifact_key:
            return False
    return True


def _cleanup_adapter_self_check():
    """Harness validation ONLY — not a product invariant. Exercises
    `_cleanup_record_matches` against fabricated, in-memory normalized
    records; never touches the DB or the real Sections B/G scenarios."""
    rec_vectors_a = {
        "state": "failed", "item_id": "it1", "user_id": "u1",
        "attempt_token": "tokA", "artifact_kind": "vectors", "artifact_key": None,
        "reason": "vector cleanup failed",
    }
    rec_s3_a = {
        "state": "pending", "item_id": "it1", "user_id": "u1",
        "attempt_token": "tokA", "artifact_kind": "s3", "artifact_key": "u1/it1.pdf",
        "reason": "archive cleanup pending",
    }
    checks = [
        ("exact attempt token + matching kind matches",
         _cleanup_record_matches(rec_vectors_a, "it1", "u1", "tokA", "vectors") is True),
        ("wrong/newer attempt token does not match",
         _cleanup_record_matches(rec_vectors_a, "it1", "u1", "tokB", "vectors") is False),
        ("S3 artifact key must match the queried key when one is given",
         _cleanup_record_matches(rec_s3_a, "it1", "u1", "tokA", "s3", artifact_key="u1/it1.pdf") is True
         and _cleanup_record_matches(rec_s3_a, "it1", "u1", "tokA", "s3", artifact_key="other/key.pdf") is False),
        ("vectors kind needs no S3 key to match",
         _cleanup_record_matches(rec_vectors_a, "it1", "u1", "tokA", "vectors", artifact_key=None) is True),
        ("s3 kind never matches a record with no artifact_key at all",
         _cleanup_record_matches(rec_vectors_a, "it1", "u1", "tokA", "s3") is False),
        ("wrong item_id/user_id never matches",
         _cleanup_record_matches(rec_vectors_a, "OTHER_ITEM", "u1", "tokA", "vectors") is False
         and _cleanup_record_matches(rec_vectors_a, "it1", "OTHER_USER", "tokA", "vectors") is False),
    ]
    ok = all(passed for _, passed in checks)
    return ok, checks


# ── Token-aware invocation adapter (Follow-up 1D, Section C) ─────────────
def _call_with_attempt_token_if_supported(fn, attempt_token, *args, **kwargs):
    """Inspects `fn`'s real callable signature; if it declares a parameter
    literally named `attempt_token`, calls it with that keyword bound to
    the given token. Otherwise calls `fn` with its legacy positional
    signature, unchanged — so a not-yet-upgraded production function is
    still exercised exactly as before, and the section stays RED. Never
    catches or reinterprets an exception `fn` itself raises (including a
    genuine TypeError from a signature this adapter mis-detected) — that
    must propagate as a real harness error, never be read as a product
    invariant result."""
    sig = inspect.signature(fn)
    if "attempt_token" in sig.parameters:
        return fn(*args, attempt_token=attempt_token, **kwargs)
    return fn(*args, **kwargs)


def _token_adapter_self_check():
    """Harness validation ONLY. Exercises
    `_call_with_attempt_token_if_supported` against two fabricated
    callables (one token-aware, one legacy) — never touches the DB or the
    real Section C scenarios."""
    calls = []

    def _token_aware_fn(item_id, message, attempt_token=None):
        calls.append(("token_aware", item_id, message, attempt_token))
        return "token-aware-ok"

    def _legacy_fn(item_id, message):
        calls.append(("legacy", item_id, message))
        return "legacy-ok"

    calls.clear()
    r1 = _call_with_attempt_token_if_supported(_token_aware_fn, "tokA", "item1", "msg1")
    check1 = calls == [("token_aware", "item1", "msg1", "tokA")] and r1 == "token-aware-ok"

    calls.clear()
    r2 = _call_with_attempt_token_if_supported(_legacy_fn, "tokA", "item1", "msg1")
    check2 = calls == [("legacy", "item1", "msg1")] and r2 == "legacy-ok"

    # Attempt B's token must never be substituted for attempt A's: calling
    # with tokA must never cause tokB to appear anywhere in the call, for
    # either callable shape.
    calls.clear()
    _call_with_attempt_token_if_supported(_token_aware_fn, "tokA", "item1", "msg1")
    check3 = calls[-1][3] == "tokA" and calls[-1][3] != "tokB"

    checks = [
        ("token-aware callable receives attempt A's exact token", check1),
        ("legacy callable remains callable with its original positional signature", check2),
        ("attempt B's token is never substituted for attempt A's", check3),
    ]
    ok = all(p for _, p in checks)
    return ok, checks


# ── Artifact-identity observer (Follow-up 1D, Section G) ─────────────────
def _observed_artifact_key(record):
    """Extracts the archive key from an ALREADY-CAPTURED, real artifact
    record (e.g. one entry from the in-memory S3 fake's own upload-call
    log, or a normalized row from a reflectively-discovered ledger) —
    returns exactly the value that was captured there. Never derives,
    hashes, or constructs a key from item_id/user_id/attempt_token itself;
    a missing record observes as None, never a fabricated key."""
    if record is None:
        return None
    return record.get("key")


def _artifact_observer_self_check():
    """Harness validation ONLY. Exercises `_observed_artifact_key` against
    fabricated, in-memory records covering both a fixed-key design (two
    captured keys that are equal) and an attempt-scoped design (two
    captured keys that differ) — never touches the DB or the real Section
    G scenario."""
    rec_fixed_a = {"key": "u1/it1.pdf", "payload": "bytes-A"}
    rec_fixed_b = {"key": "u1/it1.pdf", "payload": "bytes-B"}       # same key, fixed-key design
    rec_scoped_a = {"key": "u1/it1__attemptA.pdf", "payload": "bytes-A"}
    rec_scoped_b = {"key": "u1/it1__attemptB.pdf", "payload": "bytes-B"}  # distinct, attempt-scoped

    k_fixed_a = _observed_artifact_key(rec_fixed_a)
    k_fixed_b = _observed_artifact_key(rec_fixed_b)
    k_scoped_a = _observed_artifact_key(rec_scoped_a)
    k_scoped_b = _observed_artifact_key(rec_scoped_b)

    checks = [
        ("equal captured keys remain equal (fixed-key design case)",
         k_fixed_a == k_fixed_b and k_fixed_a is not None),
        ("two distinct captured keys remain distinct (attempt-scoped design case)",
         k_scoped_a != k_scoped_b and k_scoped_a is not None and k_scoped_b is not None),
        ("the observer returns the actual captured value rather than constructing one",
         k_fixed_a == rec_fixed_a["key"] and k_scoped_a == rec_scoped_a["key"]
         and k_scoped_b == rec_scoped_b["key"]),
        ("a missing record observes as None rather than a fabricated key",
         _observed_artifact_key(None) is None),
    ]
    ok = all(p for _, p in checks)
    return ok, checks


# ── Cleanup-retry-runner discovery predicate (Follow-up 1E, Section I2) ──
# The known, item-specific write-only helpers every A/B/F/G section already
# drives directly — never valid candidates for an AUTONOMOUS scan/retry
# runner, even though their own source genuinely references cleanup_state/
# cleanup_detail (they WRITE those fields for one already-known item; they
# don't discover or retry anything).
_CLEANUP_RETRY_EXCLUDED_NAMES = frozenset({
    "_compensate_failed_attempt",
    "_mark_cleanup_pending",
    "_resolve_cleanup_marker",
    "_cleanup_vectors_after_abandoned_processing",
    "_cleanup_archive_after_abandoned_processing",
})


def _is_cleanup_retry_candidate(name, source_text):
    """Pure predicate behind `_find_cleanup_retry_runner`: would this
    name/source combination be accepted as an autonomous cleanup-retry
    runner candidate? Rejects the known item-specific write helpers by
    name outright (regardless of what their source references), and
    otherwise requires the source to actually reference one of the durable
    cleanup field names — never accepts on name alone."""
    if name in _CLEANUP_RETRY_EXCLUDED_NAMES:
        return False
    return ("cleanup_state" in source_text) or ("cleanup_detail" in source_text)


def _cleanup_runner_discovery_self_check():
    """Harness validation ONLY. Exercises `_is_cleanup_retry_candidate`
    against fabricated name/source pairs — never touches the DB, the real
    production modules, or the real Section I2 scenario."""
    checks = [
        ("a direct item-specific compensation helper is rejected even though its own source "
         "references the durable cleanup fields",
         _is_cleanup_retry_candidate(
             "_compensate_failed_attempt",
             "item.cleanup_state = 'failed'; item.cleanup_detail = {...}") is False),
        ("every other known item-specific write helper is rejected by name",
         all(_is_cleanup_retry_candidate(n, "cleanup_state cleanup_detail") is False
             for n in _CLEANUP_RETRY_EXCLUDED_NAMES)),
        ("a genuine scan/reconcile-shaped candidate that references the durable fields is "
         "accepted",
         _is_cleanup_retry_candidate(
             "reconcile_cleanup_ledger",
             "for row in db.query(Ledger).filter(Ledger.cleanup_state == 'failed'): ...") is True),
        ("a candidate that never references the durable cleanup fields at all is rejected",
         _is_cleanup_retry_candidate("some_unrelated_function", "return 42") is False),
    ]
    ok = all(p for _, p in checks)
    return ok, checks


# ── Mixed-version write classifier (Follow-up 1E, Section J) ─────────────
def _classify_mixed_version_write(processed, entitlement_status, accounted_hint=False):
    """Pure classifier behind Section J. Given an item's `processed` flag,
    its `entitlement_status`, and an explicit hint that accounting (e.g.
    `User.successful_sources_total`) already reflects it, returns one of:
      'not_processed'                     — not relevant to this check
      'processed_and_accounted'           — a healthy, already-reconciled
                                             write (GREEN, nothing to prove)
      'processed_but_fenced'              — not (yet) accessible/successful
                                             — a legitimate GREEN outcome
                                             (DB-level fencing)
      'processed_accessible_unaccounted'  — the defect under test: looks
                                             done, IS accessible, but
                                             nothing accounts for or fences
                                             it
    Never inspects source code or schema — purely a classification of
    already-observed field values, reused identically by the real Section
    J check and by its own self-check."""
    if not processed:
        return "not_processed"
    if accounted_hint or entitlement_status in ("consumed", "premium", "grandfathered"):
        return "processed_and_accounted"
    if entitlement_status in ("rejected", "released"):
        return "processed_but_fenced"
    return "processed_accessible_unaccounted"


def _mixed_version_observer_self_check():
    """Harness validation ONLY. Exercises `_classify_mixed_version_write`
    against fabricated field-value combinations — never touches the DB or
    the real Section J scenario."""
    checks = [
        ("processed + consumed status classifies as accounted",
         _classify_mixed_version_write(True, "consumed") == "processed_and_accounted"),
        ("processed + premium status classifies as accounted",
         _classify_mixed_version_write(True, "premium") == "processed_and_accounted"),
        ("processed + an explicit accounting hint classifies as accounted even with no status",
         _classify_mixed_version_write(True, None, accounted_hint=True) == "processed_and_accounted"),
        ("processed + rejected or released status classifies as fenced",
         _classify_mixed_version_write(True, "rejected") == "processed_but_fenced"
         and _classify_mixed_version_write(True, "released") == "processed_but_fenced"),
        ("processed, no status, and no accounting hint classifies as the defect under test: "
         "accessible and unaccounted",
         _classify_mixed_version_write(True, None) == "processed_accessible_unaccounted"),
        ("an item that is not yet processed is classified separately, never as any accounted/"
         "fenced/unaccounted outcome",
         _classify_mixed_version_write(False, None) == "not_processed"
         and _classify_mixed_version_write(False, "consumed") == "not_processed"),
    ]
    ok = all(p for _, p in checks)
    return ok, checks


# ── Reporting helpers ─────────────────────────────────────────────────────
results_pass = []      # desired production invariants that HOLD right now
results_fail = []      # desired production invariants VIOLATED right now
capability_ok = []     # section E: a test-tooling capability that worked
capability_failed = []  # section E: a test-tooling capability that did NOT work
harness_errors = []    # setup/import/thread/capability problems -> exit 2


def section(t):
    print(f"\n=== {t} ===")


def observe(label, value):
    print(f"  observed: {label} = {value!r}")


def check(name, cond, detail=""):
    """`cond=True` means the DESIRED production invariant holds right now
    (a PASS). `cond=False` means it is currently violated (a FAIL) — this
    is the expected outcome against today's known-buggy code; the exact
    same call becomes a PASS after a production fix, with no edit here."""
    tag = "[PASS]" if cond else "[FAIL]"
    print(f"  {tag} {name}" + (f"  — {detail}" if detail else ""))
    (results_pass if cond else results_fail).append(name)


def check_capability(name, cond, detail=""):
    """Test-tooling capability, not a product invariant. A failure here
    means the harness itself could not faithfully set up or observe the
    scenario, so it is routed to harness_errors (exit 2), never counted as
    a failed product invariant (exit 1)."""
    tag = "[CAPABILITY OK]" if cond else "[CAPABILITY FAILED]"
    print(f"  {tag} {name}" + (f"  — {detail}" if detail else ""))
    if cond:
        capability_ok.append(name)
    else:
        capability_failed.append(name)
        harness_errors.append(f"capability: {name}")


def run_section(fn, label):
    """Runs one section in isolation: an exception here is a harness/setup
    problem for THAT section only — it is recorded distinctly from a failed
    product invariant, printed with a traceback, and does not prevent other
    (independent) sections from running."""
    try:
        fn()
    except Exception as e:
        msg = f"section {label}: {type(e).__name__}: {e}"
        print(f"\n[SETUP ERROR] {msg}")
        traceback.print_exc()
        harness_errors.append(msg)


_RESIDUE_PREFIXES = (
    "nibbler-attempt-lifecycle-repro-", "nibbler-cleanup-selfcheck-",
    "nibbler-hermetic-", "nibbler-mixed4-pg-", "nibbler-v-pg-",
)


def _snapshot_hermetic_state():
    """Correction #20: a snapshot of temp directories (by the 5 known
    prefixes), live thread names, and live PostgreSQL-related process ids
    — taken once before this run's own work starts and once after it
    ends, so `_diff_hermetic_state` can report any NEWLY created resource
    (created BY this run) that still exists/is still alive afterward,
    rather than ever touching or judging pre-existing, unrelated state."""
    tmp_root = tempfile.gettempdir()
    try:
        dirs = {
            name for name in os.listdir(tmp_root)
            if any(name.startswith(p) for p in _RESIDUE_PREFIXES)
        }
    except Exception:
        dirs = set()
    threads = {th.name for th in threading.enumerate()}
    try:
        ps_out = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True,
                                 text=True, timeout=10).stdout
        pg_pids = {
            line.split(None, 1)[0] for line in ps_out.splitlines()
            if ("postgres" in line or "initdb" in line) and "grep" not in line
        }
    except Exception:
        pg_pids = set()
    return {"dirs": dirs, "threads": threads, "pg_pids": pg_pids}


def _diff_hermetic_state(before, after):
    """New dirs (created during this run, matching a known prefix, still
    present after) / new threads (still alive after) / new PG processes
    (still present after) — each reported into `harness_errors` (never
    silently ignored), and NEVER touching anything pre-existing/unrelated.

    `nibbler-hermetic-*` is EXCLUDED from the hard "must not survive"
    check (though still snapshotted/observed) — `tests/hermetic.py` line
    41 unconditionally does `tempfile.mkdtemp(prefix="nibbler-hermetic-")`
    at MODULE IMPORT TIME with no corresponding cleanup anywhere in that
    file (confirmed by direct inspection); this is pre-existing behavior
    of a file this assignment explicitly forbids modifying, shared by
    EVERY test in this repo that imports it, not something this file
    creates or could avoid short of not importing hermetic.py at all
    (which would defeat the whole point of hermetic isolation)."""
    new_dirs_all = after["dirs"] - before["dirs"]
    new_dirs_enforced = {d for d in new_dirs_all if not d.startswith("nibbler-hermetic-")}
    new_dirs_exempted = new_dirs_all - new_dirs_enforced
    new_threads = after["threads"] - before["threads"]
    new_pg_pids = after["pg_pids"] - before["pg_pids"]
    problems = []
    if new_dirs_enforced:
        problems.append(f"{len(new_dirs_enforced)} new temp dir(s) matching a known, ENFORCED "
                         f"prefix survived: {sorted(new_dirs_enforced)!r}")
    if new_dirs_exempted:
        print(f"  [OBSERVED, not enforced — see docstring] "
              f"{len(new_dirs_exempted)} new nibbler-hermetic-* dir(s) survived "
              f"(tests/hermetic.py's own unavoidable import-time side effect): "
              f"{sorted(new_dirs_exempted)!r}")
    if new_threads:
        problems.append(f"{len(new_threads)} new thread(s) are still alive: {sorted(new_threads)!r}")
    if new_pg_pids:
        problems.append(f"{len(new_pg_pids)} new PostgreSQL-related process(es) still running: {sorted(new_pg_pids)!r}")
    return problems


def run_all():
    hermetic_state_before = _snapshot_hermetic_state()
    cleanup_ok, cleanup_path = _cleanup_self_check()
    if cleanup_ok:
        print(f"[PASS] cleanup self-check: a nested tempdir does not survive an exception "
              f"raised inside _owned_tempdir's context ({cleanup_path})")
    else:
        print(f"[SETUP ERROR] cleanup self-check: tempdir {cleanup_path!r} SURVIVED a raised "
              f"exception inside _owned_tempdir — the cleanup helper itself is broken; "
              f"aborting before doing any real work")
        harness_errors.append("cleanup self-check failed")
        return 2

    exit_ok = _exit_semantics_self_check()
    if exit_ok:
        print("[PASS] exit-semantics self-check: _decide_exit([],[])==0, "
              "_decide_exit([fail],[])==1, _decide_exit(*,[error])==2 for fabricated inputs")
    else:
        print("[SETUP ERROR] exit-semantics self-check: _decide_exit did not match the "
              "required RED=1 / GREEN=0 / SETUP-or-CAPABILITY=2 contract on fabricated inputs")
        harness_errors.append("exit-semantics self-check failed")
        return 2

    # Correction #19: `_exit_semantics_self_check` above proves `_decide_
    # exit`'s pure LOGIC on fabricated inputs, entirely within this SAME
    # interpreter — it never actually proves the real `sys.exit(run_all())`
    # boundary at the bottom of this file translates into a genuine OS
    # process exit code. This launches three real, separate subprocesses
    # to prove exactly that.
    boundary_ok, boundary_results, boundary_errors = _prove_real_process_exit_boundary()
    if boundary_ok:
        print(f"[PASS] real process-exit-boundary self-check: three genuinely separate OS "
              f"subprocesses, each forcing a synthetic green/red/setup result through the real "
              f"`sys.exit(run_all())` boundary, returned exit codes {boundary_results!r} — "
              f"exactly 0/1/2 as required")
    else:
        print(f"[SETUP ERROR] real process-exit-boundary self-check failed — "
              f"results={boundary_results!r} errors={boundary_errors!r}")
        harness_errors.append(f"real process-exit-boundary self-check failed: "
                               f"results={boundary_results!r} errors={boundary_errors!r}")
        return 2

    adapter_ok, adapter_checks = _cleanup_adapter_self_check()
    if adapter_ok:
        print("[PASS] cleanup-record adapter self-check: _cleanup_record_matches agrees with "
              "all fabricated in-memory cases (exact/wrong attempt token, S3 key match/"
              "mismatch, vectors-needs-no-key, wrong item/user)")
    else:
        print("[SETUP ERROR] cleanup-record adapter self-check: at least one fabricated case "
              "did not match the required matching contract:")
        for name, passed in adapter_checks:
            if not passed:
                print(f"    - FAILED: {name}")
        harness_errors.append("cleanup-record adapter self-check failed")
        return 2

    token_adapter_ok, token_adapter_checks = _token_adapter_self_check()
    if token_adapter_ok:
        print("[PASS] token-invocation adapter self-check: token-aware callable receives "
              "attempt A's token, legacy callable remains callable, B's token is never "
              "substituted")
    else:
        print("[SETUP ERROR] token-invocation adapter self-check: at least one fabricated "
              "case did not match the required contract:")
        for name, passed in token_adapter_checks:
            if not passed:
                print(f"    - FAILED: {name}")
        harness_errors.append("token-invocation adapter self-check failed")
        return 2

    artifact_obs_ok, artifact_obs_checks = _artifact_observer_self_check()
    if artifact_obs_ok:
        print("[PASS] artifact-identity observer self-check: equal captured keys stay equal, "
              "distinct captured keys stay distinct, and the observer returns the actual "
              "captured value rather than constructing one")
    else:
        print("[SETUP ERROR] artifact-identity observer self-check: at least one fabricated "
              "case did not match the required contract:")
        for name, passed in artifact_obs_checks:
            if not passed:
                print(f"    - FAILED: {name}")
        harness_errors.append("artifact-identity observer self-check failed")
        return 2

    runner_disc_ok, runner_disc_checks = _cleanup_runner_discovery_self_check()
    if runner_disc_ok:
        print("[PASS] cleanup-runner discovery self-check: known item-specific write helpers "
              "are rejected by name, a genuine scan/reconcile-shaped candidate is accepted, and "
              "an unrelated candidate is rejected")
    else:
        print("[SETUP ERROR] cleanup-runner discovery self-check: at least one fabricated case "
              "did not match the required contract:")
        for name, passed in runner_disc_checks:
            if not passed:
                print(f"    - FAILED: {name}")
        harness_errors.append("cleanup-runner discovery self-check failed")
        return 2

    mixed_ver_ok, mixed_ver_checks = _mixed_version_observer_self_check()
    if mixed_ver_ok:
        print("[PASS] mixed-version observer self-check: processed+accounted, processed+fenced, "
              "and processed+accessible+unaccounted are all classified distinctly")
    else:
        print("[SETUP ERROR] mixed-version observer self-check: at least one fabricated case "
              "did not match the required contract:")
        for name, passed in mixed_ver_checks:
            if not passed:
                print(f"    - FAILED: {name}")
        harness_errors.append("mixed-version observer self-check failed")
        return 2

    with _owned_tempdir("nibbler-attempt-lifecycle-repro-") as tmp_dir:
        os.environ.update(
            DATABASE_URL=f"sqlite:///{tmp_dir}/repro.db",
            CLAUDE_API_KEY="t",
            FIREBASE_PROJECT_ID="t",
        )
        try:
            import hermetic  # noqa: F401 — must precede `app.` imports; blanks provider creds
        except Exception as e:
            print(f"[SETUP ERROR] could not import hermetic.py — {e}")
            harness_errors.append(f"hermetic import failed: {e}")
            return 2

        try:
            from fastapi.testclient import TestClient
            from app.database import create_tables, SessionLocal, get_db, engine, Base
            from app.middleware.auth import get_current_user
            from app.models.user import User
            from app.models.library import LibraryItem
            import main as app_main
            from app.routers import library as library_router
            from app.services import entitlement_service as ent
            import app.services.embedding_service as es_mod
            from app.services.embedding_service import EmbeddingService
        except Exception as e:
            print(f"[SETUP ERROR] production import failed — {type(e).__name__}: {e}")
            traceback.print_exc()
            harness_errors.append(f"production import failed: {e}")
            return 2

        create_tables()
        db = SessionLocal()
        try:
            NOW = datetime.datetime.utcnow()
            OLD = NOW - datetime.timedelta(days=400)

            CURRENT_UID = {"v": None}

            def _db():
                yield db

            def _current_user():
                return db.query(User).filter(User.id == CURRENT_UID["v"]).first()

            app_main.app.dependency_overrides[get_db] = _db
            app_main.app.dependency_overrides[get_current_user] = _current_user
            _client = TestClient(app_main.app)  # kept for repo-wide fixture-convention parity

            def mkuser(uid, **kw):
                kw.setdefault("created_at", OLD)
                kw.setdefault("email", f"{uid}@example.com")
                u = User(id=uid, **kw)
                db.add(u)
                db.commit()
                return u

            def mkitem(iid, uid, **kw):
                kw.setdefault("type", "pdf")
                kw.setdefault("title", iid)
                kw.setdefault("processed", False)
                it = LibraryItem(id=iid, user_id=uid, **kw)
                db.add(it)
                db.commit()
                return it

            def refresh_item(iid):
                db.expire_all()
                return db.query(LibraryItem).filter(LibraryItem.id == iid).first()

            def refresh_user(uid):
                db.expire_all()
                return db.query(User).filter(User.id == uid).first()

            def real_reap(user_id, probe_id):
                """Trigger the REAL _reap_stale_reservations by making a genuine
                reservation attempt for a throwaway probe item under the same
                user — there is no standalone public entry point; this is the
                established, repo-wide convention (see
                tests/test_task2_remediation3.py) for exercising the real
                reaper without calling a private function directly."""
                probe = mkitem(probe_id, user_id, processed=False)
                ent.reserve_free_capacity(db, probe, user_id)

            def _poll_for_heartbeat_renewal(item_id, user_id, attempt_token, forced_stale_expiry,
                                             deadline_seconds=_HEARTBEAT_POLL_DEADLINE_SECONDS,
                                             interval_seconds=_HEARTBEAT_POLL_INTERVAL_SECONDS):
                """Repeatedly re-reads durable state for up to
                `deadline_seconds`, looking for evidence that an ACTIVE,
                still-blocked worker's OWN production heartbeat renewed its
                lease — this function never renews anything itself (no
                monkeypatching of renew_reservation_lease from the test).
                Evidence requires ALL of: same item, same user, same attempt
                token, entitlement_status=='pending', and a NEW expiry
                strictly later than `forced_stale_expiry` AND in the future
                relative to real time. Returns (renewed: bool,
                last_seen_snapshot: dict or None)."""
                deadline = time.monotonic() + deadline_seconds
                last_snapshot = None
                while True:
                    row = refresh_item(item_id)
                    if row is not None:
                        last_snapshot = {
                            "entitlement_status": row.entitlement_status,
                            "reservation_lease_token": row.reservation_lease_token,
                            "reservation_lease_expires_at": row.reservation_lease_expires_at,
                            "user_id": row.user_id,
                        }
                        renewed = (
                            row.user_id == user_id
                            and row.reservation_lease_token == attempt_token
                            and row.entitlement_status == "pending"
                            and row.reservation_lease_expires_at is not None
                            and row.reservation_lease_expires_at > forced_stale_expiry
                            and row.reservation_lease_expires_at > datetime.datetime.utcnow()
                        )
                        if renewed:
                            return True, last_snapshot
                    if time.monotonic() >= deadline:
                        return False, last_snapshot
                    time.sleep(interval_seconds)

            # ── Deterministic stale-attempt supersession barrier (2B-R1 —────
            # correction #10) ─────────────────────────────────────────────
            # Every "stale worker" section used to force a lease expiry into
            # the past and immediately call the real reaper, relying on the
            # sequence of local DB writes completing faster than the
            # attempt guard's own ~2s background heartbeat tick — a REAL
            # race, not a deterministic one (rare on a fast machine, but a
            # race nonetheless; Hermes correctly rejected this).
            #
            # `_deterministic_stale_supersession(item_id, user_id)` removes
            # the race entirely: `app.routers.library.renew_reservation_
            # lease` is patched so that ANY call originating from the
            # attempt-guard's OWN background thread (identified by its real,
            # production-assigned thread name — `_AttemptGuard.start()`
            # names it `f"attempt-guard-{item_id}"`) is a deterministic
            # no-op failure for the ENTIRE duration the patch is active,
            # while a call from any OTHER thread (a worker's own synchronous
            # checkpoint) passes through to the REAL primitive unchanged —
            # so a still-legitimate, never-superseded worker's own
            # checkpoint-driven renewal keeps working exactly as before.
            # With the background thread's renewal path guaranteed inert,
            # forcing the lease stale and calling the real reaper can never
            # race a real renewal landing in between — the outcome is
            # certain on every run, not merely likely.
            #
            # Returns a context manager; callers wrap their worker's
            # `with mock.patch(...)` block in this ONE ADDITIONAL patch
            # (composable with every other patch already in use), then, once
            # inside, call `.supersede(item_id, user_id)` to run the real
            # force-stale + real reaper + real re-reservation sequence and
            # return the new attempt's token — fully deterministic under
            # this patch.
            @contextlib.contextmanager
            def _deterministic_stale_supersession():
                real_renew = ent.renew_reservation_lease

                def _side_effect(db_arg, item_id, user_id, lease_token):
                    if threading.current_thread().name.startswith("attempt-guard-"):
                        return False
                    return real_renew(db_arg, item_id, user_id, lease_token)

                with mock.patch("app.routers.library.renew_reservation_lease", side_effect=_side_effect):
                    def _supersede(item_id, user_id, probe_id):
                        """Real force-stale + real reaper + real
                        re-reservation, deterministic under the active
                        patch above. Returns the new attempt's token."""
                        db.query(LibraryItem).filter(LibraryItem.id == item_id).update(
                            {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                        db.commit()
                        real_reap(user_id, probe_id)
                        reaped = refresh_item(item_id)
                        if reaped.entitlement_status != "released":
                            raise RuntimeError(
                                f"deterministic supersession setup failed — expected the real "
                                f"reaper to release {item_id!r} (background heartbeat renewal was "
                                f"guaranteed inert by the active patch), got "
                                f"status={reaped.entitlement_status!r}")
                        ent.reserve_free_capacity(db, reaped, user_id)
                        new_tok = refresh_item(item_id).reservation_lease_token
                        if not new_tok:
                            raise RuntimeError(
                                f"deterministic supersession setup failed — re-reservation for "
                                f"{item_id!r} did not mint a new token")
                        return new_tok
                    yield _supersede

            @contextlib.contextmanager
            def _deterministic_heartbeat_gate():
                """A finer-grained variant of `_deterministic_stale_
                supersession` for sections (L) that need ONE real
                background-thread heartbeat tick to genuinely land before
                deterministically freezing all further ones — rather than
                making the background thread inert for the WHOLE section.
                `gate.pause()` guarantees no background-thread renewal can
                COMPLETE from that call onward (any in-flight or future one
                blocks until `gate.release()`), without affecting a prior
                tick that already completed, or any WORKER-thread
                (synchronous checkpoint) call, which always passes through
                unmodified. `gate.release()` is called in every caller's own
                `finally`, so no thread is ever left blocked past the
                section that paused it."""
                real_renew = ent.renew_reservation_lease
                paused = threading.Event()
                release_gate = threading.Event()

                def _side_effect(db_arg, item_id, user_id, lease_token):
                    if threading.current_thread().name.startswith("attempt-guard-") and paused.is_set():
                        release_gate.wait(timeout=30)
                    return real_renew(db_arg, item_id, user_id, lease_token)

                class _Gate:
                    def pause(self):
                        paused.set()

                    def release(self):
                        release_gate.set()

                with mock.patch("app.routers.library.renew_reservation_lease", side_effect=_side_effect):
                    gate = _Gate()
                    try:
                        yield gate
                    finally:
                        gate.release()

            class _PostgresCapabilityError(Exception):
                """Raised for ANY PostgreSQL environment/bootstrap/teardown
                problem (2B-R1 correction #18) — missing binaries, initdb
                failure, partial startup, schema/bootstrap failure,
                inability to place a required concurrency barrier, or
                teardown failure. Callers MUST route this to
                `check_capability(...)` (exit 2), never `check(...)`
                (exit 1) — this is an environment/harness condition, not a
                product invariant result."""

            def _find_pg_bin(name):
                import shutil as _shutil_pg
                import glob as _glob_pg
                for pattern in ("/opt/homebrew/opt/postgresql@*/bin/%s" % name,
                                 "/usr/local/opt/postgresql@*/bin/%s" % name):
                    hits = sorted(_glob_pg.glob(pattern))
                    if hits:
                        return hits[-1]
                return _shutil_pg.which(name)

            @contextlib.contextmanager
            def _disposable_postgres_cluster(dir_prefix):
                """Bootstraps a real, disposable local PostgreSQL 16
                cluster with rigorous teardown discipline (correction
                #18). Yields `(engine, conn_kwargs)` — a SQLAlchemy engine
                already pointed at a fresh test database, and a dict of
                raw connection kwargs (host/port/dbname/user) any caller
                needing a SECOND, independent real connection (e.g. a
                genuine two-session concurrency test) can use directly
                with psycopg2.

                Any failure to reach a fully usable state raises
                `_PostgresCapabilityError` — the entire startup attempt is
                wrapped in its own try/except so a partial failure still
                reaches teardown. Teardown ALWAYS attempts a `pg_ctl stop`
                if a postmaster may have started (tracked via a flag set
                the instant the start command is issued, not only on
                success), then VERIFIES it actually stopped via `pg_ctl
                status` (exit non-zero means stopped), disposes every
                engine/connection this function itself opened, and only
                removes the owned directory after that verification —
                never swallowed, never skipped on an earlier exception."""
                import subprocess as _sp_pg
                import socket as _socket_pg

                initdb_bin = _find_pg_bin("initdb")
                pg_ctl_bin = _find_pg_bin("pg_ctl")
                if not initdb_bin or not pg_ctl_bin:
                    raise _PostgresCapabilityError(
                        f"PostgreSQL server binaries not found (initdb={initdb_bin!r} "
                        f"pg_ctl={pg_ctl_bin!r}) — checked versioned postgresql@NN kegs and PATH")

                pg_env = dict(os.environ)
                pg_env["LC_ALL"] = "C"  # avoids a real, reproducible macOS/Homebrew
                                        # "postmaster became multithreaded during startup" failure

                with _owned_tempdir(dir_prefix) as pg_dir:
                    data_dir = os.path.join(pg_dir, "data")
                    sock_dir = pg_dir
                    s = _socket_pg.socket(_socket_pg.AF_INET, _socket_pg.SOCK_STREAM)
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                    s.close()

                    postmaster_may_have_started = False
                    engine = None
                    try:
                        try:
                            _sp_pg.run([initdb_bin, "-D", data_dir, "-U", "postgres", "-A", "trust",
                                        "--no-sync", "--locale=C", "-E", "UTF8"],
                                       check=True, capture_output=True, timeout=60, env=pg_env)
                        except Exception as e:
                            raise _PostgresCapabilityError(f"initdb failed: {e!r}")

                        postmaster_may_have_started = True  # from HERE on, always attempt shutdown
                        try:
                            _sp_pg.run([pg_ctl_bin, "-D", data_dir, "-l", os.path.join(pg_dir, "pg.log"),
                                        "-o", f"-p {port} -k {sock_dir} -c listen_addresses=''",
                                        "-w", "start"], check=True, capture_output=True, timeout=60,
                                       env=pg_env)
                        except Exception as e:
                            log_tail = ""
                            try:
                                with open(os.path.join(pg_dir, "pg.log")) as f:
                                    log_tail = f.read()[-2000:]
                            except Exception:
                                pass
                            raise _PostgresCapabilityError(
                                f"pg_ctl start failed: {e!r} — log tail: {log_tail!r}")

                        try:
                            import psycopg2
                            conn_pg = psycopg2.connect(
                                host=sock_dir, port=port, user="postgres", dbname="postgres")
                            conn_pg.autocommit = True
                            cur = conn_pg.cursor()
                            cur.execute("CREATE DATABASE nibbler_r1_test")
                            cur.close()
                            conn_pg.close()
                        except Exception as e:
                            raise _PostgresCapabilityError(f"test database bootstrap failed: {e!r}")

                        pg_url = (f"postgresql+psycopg2://postgres@/nibbler_r1_test"
                                  f"?host={sock_dir}&port={port}")
                        from sqlalchemy import create_engine as _create_engine_pg, text as _text_pg
                        try:
                            engine = _create_engine_pg(pg_url)
                            with engine.connect() as _probe:
                                _probe.execute(_text_pg("SELECT 1"))
                        except Exception as e:
                            raise _PostgresCapabilityError(f"engine/connection bootstrap failed: {e!r}")

                        conn_kwargs = {"host": sock_dir, "port": port, "user": "postgres",
                                       "dbname": "nibbler_r1_test"}
                        yield engine, conn_kwargs
                    finally:
                        if engine is not None:
                            try:
                                engine.dispose()
                            except Exception:
                                pass
                        if postmaster_may_have_started:
                            stop_error = None
                            try:
                                _sp_pg.run([pg_ctl_bin, "-D", data_dir, "-m", "immediate", "stop"],
                                           capture_output=True, timeout=30, env=pg_env)
                            except Exception as e:
                                stop_error = e
                            # Verify shutdown — `pg_ctl status` exits non-zero
                            # once the server is genuinely stopped.
                            status_result = _sp_pg.run(
                                [pg_ctl_bin, "-D", data_dir, "status"],
                                capture_output=True, timeout=15, env=pg_env)
                            stopped = status_result.returncode != 0
                            if not stopped or stop_error is not None:
                                harness_errors.append(
                                    f"PostgreSQL teardown for {dir_prefix!r} could not be fully "
                                    f"verified — stop_error={stop_error!r} "
                                    f"status_returncode={status_result.returncode!r} — the owned "
                                    f"tempdir is still removed by _owned_tempdir's own guarantee, "
                                    f"but this is reported as a harness/capability problem, never "
                                    f"silently swallowed"
                                )
                        # `_owned_tempdir`'s own `finally: shutil.rmtree(...)`
                        # removes `pg_dir` (including `data_dir`) on the way
                        # out of this `with` block regardless — only after
                        # everything above has already run.

            def _scan_mapped_classes_for_shape(exclude_cls, required, optional):
                """Generic reflective discovery shared by the cleanup-record
                (Follow-up 1C) and artifact-identity (Follow-up 1D) adapters:
                scans every SQLAlchemy-mapped class OTHER than `exclude_cls`
                for the first one whose columns satisfy every REQUIRED
                semantic field category — never a specific class/table/
                column name, only field SHAPE, using plausible attribute-
                name variants drawn from this codebase's own established
                vocabulary. `required`/`optional` are dicts of
                category_name -> tuple of acceptable column-name variants;
                optional categories are filled in when present, else None.
                Returns (model_class, {category: column_name}), or
                (None, None) if nothing matches — the correct, expected
                outcome against current code, which has neither a separate
                cleanup ledger nor a separate artifact ledger yet."""
                def _pick(cols, names):
                    for n in names:
                        if n in cols:
                            return n
                    return None

                for mapper in list(Base.registry.mappers):
                    cls = mapper.class_
                    if cls is exclude_cls:
                        continue
                    cols = {c.key for c in mapper.columns}
                    fields = {}
                    ok = True
                    for cat, names in required.items():
                        picked = _pick(cols, names)
                        if not picked:
                            ok = False
                            break
                        fields[cat] = picked
                    if not ok:
                        continue
                    for cat, names in optional.items():
                        fields[cat] = _pick(cols, names)
                    return cls, fields
                return None, None

            def _find_cleanup_ledger_model():
                """Reflectively looks for a durable cleanup-ledger model
                SEPARATE from LibraryItem — see `_scan_mapped_classes_for_
                shape`. A candidate must expose columns identifying: which
                item, which user, which attempt, what kind of artifact, and
                a state (artifact key and a reason are optional)."""
                return _scan_mapped_classes_for_shape(
                    LibraryItem,
                    required={
                        "item": ("library_item_id", "item_id"),
                        "user": ("user_id",),
                        "attempt": ("attempt_token",),
                        "kind": ("artifact_kind", "kind", "artifact_type"),
                        "state": ("state", "cleanup_state", "status"),
                    },
                    optional={
                        "key": ("artifact_key", "s3_key", "key"),
                        "reason": ("reason", "detail", "message"),
                    },
                )

            def _find_artifact_ledger_model():
                """Reflectively looks for a durable ARTIFACT-IDENTITY ledger
                model SEPARATE from LibraryItem — see
                `_scan_mapped_classes_for_shape`. A candidate must expose
                columns identifying: which item, which user, which attempt,
                what kind of artifact, and the key itself. No state column
                is required — an artifact-identity record only needs to say
                which key an attempt used, not whether cleanup happened."""
                return _scan_mapped_classes_for_shape(
                    LibraryItem,
                    required={
                        "item": ("library_item_id", "item_id"),
                        "user": ("user_id",),
                        "attempt": ("attempt_token",),
                        "kind": ("artifact_kind", "kind", "artifact_type"),
                        "key": ("artifact_key", "s3_key", "key"),
                    },
                    optional={},
                )

            def find_artifact_key(item_id, user_id, attempt_token, artifact_kind="s3", observed_upload=None):
                """Semantic adapter (Follow-up 1D) for the archive identity
                a given attempt's real production archive/upload call
                actually produced — never a key this test constructs.
                Prefers, in order:
                  1. `observed_upload` — a record captured directly from the
                     in-memory S3 fake's own upload-call log, normalized via
                     `_observed_artifact_key` — the most direct signal,
                     since it reflects exactly what production's own upload
                     call was given, uncontaminated by a later attempt
                     overwriting a shared row field.
                  2. the current LibraryItem row's `file_url`, when it still
                     belongs to the queried attempt
                     (`last_processing_attempt_id == attempt_token`) — the
                     correct source for a FIXED-key design, where the row
                     only ever holds the CURRENT attempt's key.
                  3. a reflectively-discovered, separate artifact ledger (if
                     a future implementation adds one) — the correct source
                     for retroactively observing an ATTEMPT-SCOPED design's
                     key after supersession.
                Returns None, never a guessed/constructed key, if no source
                has one."""
                observed = _observed_artifact_key(observed_upload)
                if observed is not None:
                    return observed

                db.expire_all()
                item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
                if (
                    item is not None
                    and item.user_id == user_id
                    and item.last_processing_attempt_id == attempt_token
                    and item.file_url
                ):
                    return item.file_url

                ledger_cls, fields = _find_artifact_ledger_model()
                if ledger_cls is not None:
                    row = db.query(ledger_cls).filter(
                        getattr(ledger_cls, fields["item"]) == item_id,
                        getattr(ledger_cls, fields["user"]) == user_id,
                        getattr(ledger_cls, fields["attempt"]) == attempt_token,
                        getattr(ledger_cls, fields["kind"]) == artifact_kind,
                    ).first()
                    if row is not None:
                        return getattr(row, fields["key"])
                return None

            def find_cleanup_record(item_id, user_id, attempt_token, artifact_kind, artifact_key=None):
                """Semantic adapter over durable cleanup state (Follow-up
                1C): normalizes EITHER a correctly-owned LibraryItem-row
                marker OR a row from a separate cleanup-ledger model (if a
                future implementation adds one) into the shape described in
                the module docstring, then returns the first normalized
                candidate that matches the query per
                `_cleanup_record_matches` — the SAME predicate the adapter
                self-check exercises on fabricated data. Returns None if no
                durable record matches under either representation, which
                is the correct, current-code outcome for a superseded
                attempt with no separate ledger yet.

                Queries REAL durable database state only — never inserts,
                never fabricates. Section B/G call this AFTER driving the
                real compensation path, exactly like every other read in
                this file."""
                db.expire_all()
                candidates = []

                item = db.query(LibraryItem).filter(LibraryItem.id == item_id).first()
                if item is not None and isinstance(item.cleanup_detail, dict):
                    s3_key_on_row = item.cleanup_detail.get("s3_key")
                    row_attempt = item.cleanup_detail.get("attempt_token")
                    row_reason = item.cleanup_detail.get("reason")
                    if s3_key_on_row:
                        candidates.append({
                            "state": item.cleanup_state, "item_id": item.id, "user_id": item.user_id,
                            "attempt_token": row_attempt, "artifact_kind": "s3",
                            "artifact_key": s3_key_on_row, "reason": row_reason,
                        })
                    # The combined marker covers vector cleanup UNCONDITIONALLY
                    # — every _compensate_failed_attempt call attempts it —
                    # so it is always a valid "vectors" candidate too,
                    # independent of whether an s3_key also happens to be set.
                    candidates.append({
                        "state": item.cleanup_state, "item_id": item.id, "user_id": item.user_id,
                        "attempt_token": row_attempt, "artifact_kind": "vectors",
                        "artifact_key": None, "reason": row_reason,
                    })

                ledger_cls, fields = _find_cleanup_ledger_model()
                if ledger_cls is not None:
                    q = db.query(ledger_cls).filter(
                        getattr(ledger_cls, fields["item"]) == item_id,
                        getattr(ledger_cls, fields["user"]) == user_id,
                    )
                    for row in q.all():
                        candidates.append({
                            "state": getattr(row, fields["state"]),
                            "item_id": item_id, "user_id": user_id,
                            "attempt_token": getattr(row, fields["attempt"]),
                            "artifact_kind": getattr(row, fields["kind"]),
                            "artifact_key": getattr(row, fields["key"]) if fields["key"] else None,
                            "reason": getattr(row, fields["reason"]) if fields["reason"] else None,
                        })

                for rec in candidates:
                    if _cleanup_record_matches(rec, item_id, user_id, attempt_token, artifact_kind, artifact_key):
                        return rec
                return None

            def _find_cleanup_retry_runner():
                """Reflectively searches for a production callable that
                AUTONOMOUSLY retries durable cleanup_state/cleanup_detail
                work — a scheduled service, startup/background
                reconciliation hook, queue runner, or scan function — by
                SEMANTIC CAPABILITY, never by one guessed symbol name (see
                `_is_cleanup_retry_candidate`). Scans every module-level
                function in the two modules that own this durable state
                (`app.services.entitlement_service`, `app.routers.library`).
                Returns the callable, or None — None is a legitimate,
                correctly-detected outcome when no such mechanism exists."""
                for module in (ent, library_router):
                    for name, fn in inspect.getmembers(module, inspect.isfunction):
                        try:
                            src = inspect.getsource(fn)
                        except (OSError, TypeError):
                            continue
                        if _is_cleanup_retry_candidate(name, src):
                            return fn
                return None

            # ═════════════════════════════════════════════════════════════
            def _section_a():
                section("A — Pinecone false-return cleanup failure")
                mkuser("repro_a_u", successful_sources_total=0)
                it_a = mkitem("repro_a_item", "repro_a_u", processed=False)
                ok_a = ent.reserve_free_capacity(db, it_a, "repro_a_u")
                tok_a = refresh_item("repro_a_item").reservation_lease_token
                observe("reservation accepted", ok_a)
                observe("attempt token", tok_a)
                if not (ok_a and tok_a):
                    raise RuntimeError("reservation fixture did not produce a token")

                with mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
                    # delete_item_vectors reports failure via its RETURN VALUE,
                    # not an exception — exactly what a real Pinecone client
                    # call returning a non-exception failed response looks
                    # like by the time it reaches this call site.
                    MockEmbed.return_value.delete_item_vectors.return_value = False
                    library_router._compensate_failed_attempt(
                        "repro_a_item", "repro_a_u", attempt_token=tok_a, reason="section A repro")

                after_a = refresh_item("repro_a_item")
                observe("cleanup_state after a False (non-raising) cleanup failure", after_a.cleanup_state)
                observe("cleanup_detail after a False (non-raising) cleanup failure", after_a.cleanup_detail)

                invariant_a_holds = (
                    after_a.cleanup_state == "failed"
                    and isinstance(after_a.cleanup_detail, dict)
                    and after_a.cleanup_detail.get("attempt_token") == tok_a
                )
                check(
                    "a delete_item_vectors() False return is durably recorded as a cleanup "
                    "failure (cleanup_state=='failed', exact attempt identity retained)",
                    invariant_a_holds,
                    detail=(
                        f"cleanup_state={after_a.cleanup_state!r} cleanup_detail={after_a.cleanup_detail!r} "
                        f"— _compensate_failed_attempt's `try/except Exception: ok=False` only reacts "
                        f"to a RAISE; a plain False return is not an exception, so `ok` stays True and "
                        f"_resolve_cleanup_marker(...,ok=True) clears the marker instead of failing it"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_b():
                section("B — Stale-attempt cleanup failure identity")
                mkuser("repro_b_u", successful_sources_total=0)
                it_b = mkitem("repro_b_item", "repro_b_u", processed=False)
                ent.reserve_free_capacity(db, it_b, "repro_b_u")
                tok_attempt_a = refresh_item("repro_b_item").reservation_lease_token
                observe("attempt A token", tok_attempt_a)

                db.query(LibraryItem).filter(LibraryItem.id == "repro_b_item").update(
                    {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                db.commit()
                real_reap("repro_b_u", "repro_b_probe1")
                reaped_status_b = refresh_item("repro_b_item").entitlement_status
                observe("repro_b_item status after the real reaper ran", reaped_status_b)
                if reaped_status_b != "released":
                    raise RuntimeError(
                        f"expected the real reaper to release repro_b_item, got "
                        f"status={reaped_status_b!r} — fixture/timing assumption is wrong")

                it_b_retry = refresh_item("repro_b_item")
                ent.reserve_free_capacity(db, it_b_retry, "repro_b_u")
                tok_attempt_b = refresh_item("repro_b_item").reservation_lease_token
                observe("attempt B token (must differ from A)", tok_attempt_b)
                if tok_attempt_b == tok_attempt_a or not tok_attempt_b:
                    raise RuntimeError("attempt B did not mint a genuinely new token")

                b_marker_content = "attempt-B's own real content, must survive"
                db.query(LibraryItem).filter(LibraryItem.id == "repro_b_item").update(
                    {"content": b_marker_content})
                db.commit()

                with mock.patch("app.routers.library.EmbeddingService") as MockEmbed:
                    MockEmbed.return_value.delete_item_vectors.return_value = False
                    library_router._compensate_failed_attempt(
                        "repro_b_item", "repro_b_u", attempt_token=tok_attempt_a,
                        reason="stale attempt A cleanup, section B repro")

                after_b = refresh_item("repro_b_item")
                observe("cleanup_state after attempt A's late, failed cleanup", after_b.cleanup_state)
                observe("cleanup_detail after attempt A's late, failed cleanup", after_b.cleanup_detail)
                observe("item content (must still be attempt B's)", after_b.content)
                observe("item reservation_lease_token (must still be attempt B's)", after_b.reservation_lease_token)

                rec_a = find_cleanup_record("repro_b_item", "repro_b_u", tok_attempt_a, "vectors")
                observe("cleanup record found for attempt A (kind=vectors, via find_cleanup_record)", rec_a)
                check(
                    "attempt A's cleanup failure has a durable, independently retryable record — "
                    "as EITHER a correctly-owned item marker OR a separate attempt-scoped cleanup "
                    "ledger — even though it no longer owns the row",
                    rec_a is not None and rec_a.get("state") in ("pending", "failed"),
                    detail=(
                        f"find_cleanup_record(...)={rec_a!r} — _mark_cleanup_pending's ownership "
                        f"check (`item.last_processing_attempt_id == attempt_token`) correctly refuses to "
                        f"write attempt A's marker onto attempt B's row, and current production code has "
                        f"no SEPARATE cleanup-ledger model either (the reflective mapper scan finds none), "
                        f"so A's failure is not recorded anywhere under either representation this adapter "
                        f"accepts — not durable, not retryable"
                    ),
                )
                rec_b_falsely_blamed = find_cleanup_record("repro_b_item", "repro_b_u", tok_attempt_b, "vectors")
                observe("cleanup record found for attempt B (kind=vectors) — must be None, B never failed anything",
                        rec_b_falsely_blamed)
                check(
                    "no cleanup record was left falsely claiming ownership of, or attributing "
                    "A's failure to, attempt B",
                    rec_b_falsely_blamed is None,
                )
                check(
                    "attempt B's reservation token and content remain exactly as B left them",
                    after_b.content == b_marker_content and after_b.reservation_lease_token == tok_attempt_b,
                )

            # ═════════════════════════════════════════════════════════════
            def _section_c():
                section("C — Stale worker error mutation")
                # C1: _record_processing_error, invoked via the token-aware
                # adapter — attempt A's token is captured and preserved
                # BEFORE supersession, and is the ONLY token ever offered to
                # this stale callback; it is never substituted with B's.
                mkuser("repro_c_u", successful_sources_total=0)
                it_c = mkitem("repro_c_item", "repro_c_u", processed=False)
                ent.reserve_free_capacity(db, it_c, "repro_c_u")
                tok_c_a = refresh_item("repro_c_item").reservation_lease_token
                observe("attempt A token (C1), preserved before supersession", tok_c_a)
                if not tok_c_a:
                    raise RuntimeError("reservation fixture did not produce a token")

                db.query(LibraryItem).filter(LibraryItem.id == "repro_c_item").update(
                    {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                db.commit()
                real_reap("repro_c_u", "repro_c_probe1")
                if refresh_item("repro_c_item").entitlement_status != "released":
                    raise RuntimeError("expected the real reaper to release repro_c_item")
                it_c_retry = refresh_item("repro_c_item")
                ent.reserve_free_capacity(db, it_c_retry, "repro_c_u")
                tok_c_b = refresh_item("repro_c_item").reservation_lease_token
                observe("attempt B token (C1, must differ from A)", tok_c_b)
                if tok_c_b == tok_c_a or not tok_c_b:
                    raise RuntimeError("attempt B did not mint a genuinely new token")
                db.query(LibraryItem).filter(LibraryItem.id == "repro_c_item").update(
                    {"processing_error": None})  # attempt B's own clean state
                db.commit()

                _call_with_attempt_token_if_supported(
                    library_router._record_processing_error, tok_c_a,
                    "repro_c_item", "attempt A's stale error message")

                after_c1 = refresh_item("repro_c_item")
                observe("processing_error after attempt A's stale _record_processing_error call",
                        after_c1.processing_error)
                check(
                    "_record_processing_error refuses to mutate an item no longer owned by the "
                    "calling attempt, when invoked with attempt A's own preserved token via the "
                    "token-aware adapter",
                    after_c1.processing_error != "attempt A's stale error message",
                    detail=(
                        f"current signature is (item_id, message) — no attempt_token parameter "
                        f"exists, so the token-aware adapter falls back to this legacy call "
                        f"unchanged and it cannot refuse anything; it unconditionally overwrites "
                        f"processing_error for any item_id regardless of current ownership "
                        f"(tok_a={tok_c_a!r} was preserved and available, but never accepted)"
                    ),
                )

                # C2: OCR's inline exception-handler direct write — reproduced by
                # driving the REAL _run_ocr (via the same token-aware adapter,
                # with attempt A's own preserved token) through a genuine
                # supersession that happens WHILE the (mocked) OCR call is in
                # flight, then letting that same call raise.
                mkuser("repro_c2_u", successful_sources_total=0)
                it_c2 = mkitem("repro_c2_item", "repro_c2_u", processed=False, file_url="s3://x")
                ent.reserve_free_capacity(db, it_c2, "repro_c2_u")
                tok_c2_a = refresh_item("repro_c2_item").reservation_lease_token
                observe("attempt A token (C2), preserved before supersession", tok_c2_a)
                if not tok_c2_a:
                    raise RuntimeError("reservation fixture did not produce a token")

                def _supersede_then_fail(*_a, **_kw):
                    db.query(LibraryItem).filter(LibraryItem.id == "repro_c2_item").update(
                        {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                    db.commit()
                    real_reap("repro_c2_u", "repro_c2_probe1")
                    if refresh_item("repro_c2_item").entitlement_status != "released":
                        raise AssertionError("reaper did not release repro_c2_item")
                    it_retry = refresh_item("repro_c2_item")
                    ent.reserve_free_capacity(db, it_retry, "repro_c2_u")
                    db.query(LibraryItem).filter(LibraryItem.id == "repro_c2_item").update(
                        {"ocr_status": "running", "processing_error": None})  # B's clean state
                    db.commit()
                    raise RuntimeError("attempt A's OCR call failed after being superseded")

                with mock.patch("app.routers.library.S3Service") as MockS3, \
                     mock.patch("app.services.ocr_service.ocr_pdf", side_effect=_supersede_then_fail):
                    MockS3.return_value.download_file.return_value = b"fake-pdf-bytes"
                    _call_with_attempt_token_if_supported(
                        library_router._run_ocr, tok_c2_a, "repro_c2_item", "repro_c2_u")

                after_c2 = refresh_item("repro_c2_item")
                observe("ocr_status after attempt A's late exception handler ran", after_c2.ocr_status)
                observe("processing_error after attempt A's late exception handler ran", after_c2.processing_error)
                check(
                    "_run_ocr's inline exception handler refuses to mutate ocr_status/"
                    "processing_error on an item no longer owned by the failing attempt, when "
                    "invoked with attempt A's own preserved token via the token-aware adapter",
                    "attempt A" not in (after_c2.processing_error or ""),
                    detail=(
                        f"ocr_status={after_c2.ocr_status!r} processing_error={after_c2.processing_error!r} "
                        f"— current signature is (item_id, user_id) — no attempt_token parameter "
                        f"exists, so the token-aware adapter falls back to this legacy call "
                        f"unchanged; the except-block sets item.ocr_status='failed' and "
                        f"item.processing_error unconditionally BEFORE any lease_token/"
                        f"attempt_token comparison (tok_a={tok_c2_a!r} was preserved and "
                        f"available, but never accepted)"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_d():
                section("D — Lease coverage gap before the first embedding batch")

                class _FakeIndex:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        # Reached by a stale worker's own exception-handling
                        # cleanup path; a real no-op keeps that from generating
                        # a confusing secondary error unrelated to this section.
                        pass

                def _fake_embedding_service_init(self):
                    self.pinecone_available = True
                    self.index = _FakeIndex()

                def _blocking_get_embeddings(reached_evt, release_evt, dim=4):
                    """Signals `reached_evt` the moment it's entered (proving the
                    worker is genuinely inside the pre-batch phase), then BLOCKS
                    on `release_evt` — a real, indefinite pause — before
                    returning a deterministic embedding per input text."""
                    def _fn(texts):
                        reached_evt.set()
                        release_evt.wait(timeout=10)
                        return [[0.1] * dim for _ in texts]
                    return _fn

                # --- D1: plain text pipeline ------------------------------
                mkuser("repro_d1_u", successful_sources_total=0)
                mkitem("repro_d1_item", "repro_d1_u", processed=False, content="some real content to embed")
                it_d1 = refresh_item("repro_d1_item")
                ent.reserve_free_capacity(db, it_d1, "repro_d1_u")
                tok_d1 = refresh_item("repro_d1_item").reservation_lease_token
                observe("D1 (text) attempt token before the worker starts", tok_d1)

                reached_d1 = threading.Event()
                release_d1 = threading.Event()
                worker_exc_d1 = []

                def _run_text_worker():
                    try:
                        library_router.process_item_embeddings("repro_d1_item", "repro_d1_u")
                    except Exception as e:
                        worker_exc_d1.append(e)

                with mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                     mock.patch.object(es_mod, "_get_embeddings", side_effect=_blocking_get_embeddings(reached_d1, release_d1)):
                    t_d1 = threading.Thread(target=_run_text_worker)
                    t_d1.start()
                    try:
                        got_reached_d1 = reached_d1.wait(timeout=10)
                        observe("worker genuinely entered _get_embeddings (pre-batch phase)", got_reached_d1)
                        if not got_reached_d1:
                            raise RuntimeError("worker never reached _get_embeddings")

                        initial_expiry_d1 = refresh_item("repro_d1_item").reservation_lease_expires_at
                        observe("initial lease expiry, recorded before forcing a stale value", initial_expiry_d1)
                        forced_stale_expiry_d1 = NOW - datetime.timedelta(seconds=1)
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_d1_item").update(
                            {"reservation_lease_expires_at": forced_stale_expiry_d1})
                        db.commit()
                        observe("lease expiry forced into an expired state, worker still blocked", forced_stale_expiry_d1)

                        heartbeat_observed_d1, snapshot_d1 = _poll_for_heartbeat_renewal(
                            "repro_d1_item", "repro_d1_u", tok_d1, forced_stale_expiry_d1)
                        observe("heartbeat observed before the reaper ran (text)", heartbeat_observed_d1)
                        observe("row snapshot at end of the heartbeat poll window (text)", snapshot_d1)

                        real_reap("repro_d1_u", "repro_d1_probe1")
                        status_during_block_d1 = refresh_item("repro_d1_item").entitlement_status
                        observe("entitlement_status WHILE the worker is still blocked in _get_embeddings, "
                                "after the reaper ran", status_during_block_d1)
                    finally:
                        release_d1.set()
                    t_d1.join(timeout=10)

                if worker_exc_d1:
                    raise RuntimeError(f"worker thread raised {worker_exc_d1[0]!r}")
                if t_d1.is_alive():
                    raise RuntimeError("D1 worker thread did not finish within its join timeout")

                after_d1 = refresh_item("repro_d1_item")
                observe("final entitlement_status (D1, text)", after_d1.entitlement_status)
                observe("final processed flag (D1, text)", after_d1.processed)

                check(
                    "(text) an active worker's production heartbeat renews the lease during the "
                    "long pre-batch provider phase, before the reaper ever runs",
                    heartbeat_observed_d1,
                    detail=(
                        f"heartbeat_observed={heartbeat_observed_d1!r} snapshot={snapshot_d1!r} — "
                        f"process_item_embeddings calls index_text() with NO on_batch heartbeat "
                        f"argument at all, so nothing renews the lease during _get_embeddings() "
                        f"within the {_HEARTBEAT_POLL_DEADLINE_SECONDS}s poll window"
                    ),
                )
                check(
                    "(text) the reservation survives the reaper while the worker is genuinely "
                    "still blocked in the pre-batch provider phase",
                    status_during_block_d1 == "pending",
                    detail=f"status_during_block={status_during_block_d1!r}",
                )
                consumed_without_heartbeat_d1 = (
                    after_d1.entitlement_status == "consumed" and not heartbeat_observed_d1
                )
                check(
                    "(text) the worker's own eventual finalize never converts a reservation into "
                    "'consumed' unless it was actually kept alive by a real heartbeat (a worker "
                    "that was genuinely reaped must never succeed with its now-superseded token; "
                    "a worker that was genuinely heartbeat-protected is legitimately entitled to)",
                    not consumed_without_heartbeat_d1,
                    detail=(
                        f"entitlement_status={after_d1.entitlement_status!r} "
                        f"heartbeat_observed={heartbeat_observed_d1!r} processed={after_d1.processed!r}"
                    ),
                )

                # --- D2: PDF pipeline (archive/extraction pipeline) -------
                mkuser("repro_d2_u", successful_sources_total=0)
                mkitem("repro_d2_item", "repro_d2_u", processed=False)
                it_d2 = refresh_item("repro_d2_item")
                ent.reserve_free_capacity(db, it_d2, "repro_d2_u")
                tok_d2 = refresh_item("repro_d2_item").reservation_lease_token
                observe("D2 (PDF) attempt token before the worker starts", tok_d2)

                reached_d2 = threading.Event()
                release_d2 = threading.Event()
                worker_exc_d2 = []

                def _run_pdf_worker():
                    try:
                        library_router.process_pdf_embeddings("repro_d2_item", b"%PDF-fake-bytes", "repro_d2_u")
                    except Exception as e:
                        worker_exc_d2.append(e)

                with mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                     mock.patch.object(es_mod, "_get_embeddings", side_effect=_blocking_get_embeddings(reached_d2, release_d2)), \
                     mock.patch("app.routers.library.S3Service") as MockS3_d2, \
                     mock.patch("app.services.text_extract.pdf_to_structured_text",
                                 return_value="some real extracted PDF text, long enough to index"):
                    MockS3_d2.return_value.upload_file.return_value = "repro_d2_u/repro_d2_item.pdf"
                    t_d2 = threading.Thread(target=_run_pdf_worker)
                    t_d2.start()
                    try:
                        got_reached_d2 = reached_d2.wait(timeout=10)
                        observe("worker (PDF) genuinely entered _get_embeddings (pre-batch phase)", got_reached_d2)
                        if not got_reached_d2:
                            raise RuntimeError("PDF worker never reached _get_embeddings")

                        initial_expiry_d2 = refresh_item("repro_d2_item").reservation_lease_expires_at
                        observe("initial lease expiry, recorded before forcing a stale value (PDF)", initial_expiry_d2)
                        forced_stale_expiry_d2 = NOW - datetime.timedelta(seconds=1)
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_d2_item").update(
                            {"reservation_lease_expires_at": forced_stale_expiry_d2})
                        db.commit()
                        observe("lease expiry forced into an expired state, worker still blocked (PDF)",
                                forced_stale_expiry_d2)

                        heartbeat_observed_d2, snapshot_d2 = _poll_for_heartbeat_renewal(
                            "repro_d2_item", "repro_d2_u", tok_d2, forced_stale_expiry_d2)
                        observe("heartbeat observed before the reaper ran (PDF)", heartbeat_observed_d2)
                        observe("row snapshot at end of the heartbeat poll window (PDF)", snapshot_d2)

                        real_reap("repro_d2_u", "repro_d2_probe1")
                        status_during_block_d2 = refresh_item("repro_d2_item").entitlement_status
                        observe("entitlement_status WHILE the PDF worker is still blocked in "
                                "_get_embeddings, after the reaper ran", status_during_block_d2)
                    finally:
                        release_d2.set()
                    t_d2.join(timeout=10)

                if worker_exc_d2:
                    raise RuntimeError(f"PDF worker thread raised {worker_exc_d2[0]!r}")
                if t_d2.is_alive():
                    raise RuntimeError("D2 worker thread did not finish within its join timeout")

                after_d2 = refresh_item("repro_d2_item")
                observe("final entitlement_status (D2, PDF)", after_d2.entitlement_status)
                observe("final content (D2, PDF) — must be None if the stale attempt correctly could not finalize",
                        after_d2.content)

                check(
                    "(PDF) an active worker's production heartbeat renews the lease during the "
                    "long pre-batch provider phase, before the reaper ever runs, even though this "
                    "pipeline HAS on_batch heartbeat wiring for later batches",
                    heartbeat_observed_d2,
                    detail=(
                        f"heartbeat_observed={heartbeat_observed_d2!r} snapshot={snapshot_d2!r} — "
                        f"PDF's on_batch heartbeat only fires INSIDE index_text's upsert loop, "
                        f"strictly AFTER _get_embeddings() has already returned, so nothing renews "
                        f"the lease within the {_HEARTBEAT_POLL_DEADLINE_SECONDS}s poll window"
                    ),
                )
                check(
                    "(PDF) the reservation survives the reaper while the worker is genuinely still "
                    "blocked in the pre-batch provider phase",
                    status_during_block_d2 == "pending",
                    detail=f"status_during_block={status_during_block_d2!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_e():
                section("E — PG14 harness capability check (scan-vs-lock barrier placement)")

                mkuser("repro_e_u", successful_sources_total=0)
                it_e = mkitem("repro_e_item", "repro_e_u", processed=False)
                ent.reserve_free_capacity(db, it_e, "repro_e_u")
                tok_e = refresh_item("repro_e_item").reservation_lease_token
                db.query(LibraryItem).filter(LibraryItem.id == "repro_e_item").update(
                    {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                db.commit()

                event_log = []

                class _BarrierSession:
                    def __init__(self, real_session, on_after_scan):
                        self._real = real_session
                        self._on_after_scan = on_after_scan
                        self._scan_seen = False

                    def query(self, *entities, **kw):
                        q = self._real.query(*entities, **kw)
                        is_scan_query = (
                            not self._scan_seen and len(entities) == 1 and entities[0] is LibraryItem.id
                        )
                        is_lock_query = (
                            self._scan_seen and len(entities) == 1 and entities[0] is LibraryItem
                        )
                        if is_scan_query:
                            self._scan_seen = True
                            orig_all = q.all

                            def _wrapped_all(*a, **kw2):
                                event_log.append("scan_all_called")
                                result = orig_all(*a, **kw2)
                                event_log.append("scan_all_returned")
                                self._on_after_scan()
                                event_log.append("barrier_fired")
                                return result
                            q.all = _wrapped_all
                        elif is_lock_query:
                            event_log.append("lock_query_called")
                            orig_all2 = q.all

                            def _wrapped_all2(*a, **kw2):
                                result = orig_all2(*a, **kw2)
                                event_log.append("lock_all_returned")
                                return result
                            q.all = _wrapped_all2
                        return q

                    def __getattr__(self, name):
                        return getattr(self._real, name)

                def _on_after_scan_barrier():
                    s2 = SessionLocal()
                    try:
                        ok = ent.renew_reservation_lease(s2, "repro_e_item", "repro_e_u", tok_e)
                        event_log.append(f"renew_from_separate_session={ok}")
                    finally:
                        s2.close()

                barrier_session = _BarrierSession(db, _on_after_scan_barrier)
                user_e = refresh_user("repro_e_u")
                ent._reap_stale_reservations(barrier_session, user_e)
                db.commit()

                observe("event log (proves call order: scan before barrier before lock)", event_log)
                barrier_between_scan_and_lock = (
                    "scan_all_returned" in event_log
                    and "barrier_fired" in event_log
                    and "lock_query_called" in event_log
                    and event_log.index("scan_all_returned") < event_log.index("barrier_fired")
                    and event_log.index("barrier_fired") < event_log.index("lock_query_called")
                )
                check_capability(
                    "a synchronization barrier CAN be placed after the unlocked candidate scan but "
                    "before row-lock acquisition, via session instrumentation, with no edit to "
                    "entitlement_service.py",
                    barrier_between_scan_and_lock,
                    detail=f"event_log={event_log}",
                )

                final_e = refresh_item("repro_e_item")
                observe("repro_e_item status after a renewal landed precisely in that window", final_e.entitlement_status)
                check_capability(
                    "a renewal performed exactly in that narrower window is honored by the real "
                    "_reap_stale_reservations() re-check-under-lock",
                    final_e.entitlement_status == "pending",
                    detail=f"entitlement_status={final_e.entitlement_status!r}",
                )

                print(
                    "  [NOTE] PG14's own barrier (tests/test_task2_pg_harness.py, unmodified, not "
                    "re-executed here) wraps the WHOLE _reap_stale_reservations() call before it "
                    "starts — i.e. before the unlocked candidate scan too, not narrowly between the "
                    "scan and the row-lock acquisition. This section's barrier is strictly more precise."
                )

            # ═════════════════════════════════════════════════════════════
            def _section_f():
                section("F — S3 false-return cleanup failure")

                mkuser("repro_f_u", successful_sources_total=0)
                it_f = mkitem("repro_f_item", "repro_f_u", processed=False)
                ent.reserve_free_capacity(db, it_f, "repro_f_u")
                tok_f = refresh_item("repro_f_item").reservation_lease_token
                s3_key_f = "repro_f_u/repro_f_item.pdf"
                db.query(LibraryItem).filter(LibraryItem.id == "repro_f_item").update(
                    {"file_url": s3_key_f, "archive_status": "archived"})
                db.commit()
                observe("attempt token", tok_f)
                observe("archive key owned by this attempt", s3_key_f)
                if not tok_f:
                    raise RuntimeError("reservation fixture did not produce a token")

                # EmbeddingService is left REAL (unmocked): in this hermetic
                # environment pinecone_available is False (no Pinecone key
                # configured), so delete_item_vectors() returns True with zero
                # network calls — vector cleanup succeeds, isolating S3
                # behavior as the only variable under test, per Step 4.4.
                with mock.patch("app.routers.library.S3Service") as MockS3:
                    MockS3.return_value.delete_file.return_value = False
                    library_router._compensate_failed_attempt(
                        "repro_f_item", "repro_f_u", attempt_token=tok_f, s3_key=s3_key_f,
                        reason="section F repro")

                after_f = refresh_item("repro_f_item")
                observe("cleanup_state after a False (non-raising) S3 delete_file failure", after_f.cleanup_state)
                observe("cleanup_detail after a False (non-raising) S3 delete_file failure", after_f.cleanup_detail)
                observe("file_url (must be retained for retry, not cleared as if removed)", after_f.file_url)
                observe("archive_status", after_f.archive_status)

                invariant_f_holds = (
                    after_f.cleanup_state == "failed"
                    and isinstance(after_f.cleanup_detail, dict)
                    and after_f.cleanup_detail.get("attempt_token") == tok_f
                    and after_f.file_url == s3_key_f
                )
                check(
                    "an S3 delete_file() False return is durably recorded as a cleanup failure, and "
                    "the archive key/identity is retained for retry rather than the row falsely "
                    "claiming the file was removed",
                    invariant_f_holds,
                    detail=(
                        f"cleanup_state={after_f.cleanup_state!r} cleanup_detail={after_f.cleanup_detail!r} "
                        f"file_url={after_f.file_url!r} archive_status={after_f.archive_status!r} — "
                        f"_cleanup_archive_after_abandoned_processing never inspects delete_file()'s "
                        f"return value: it calls S3Service().delete_file(s3_key) and unconditionally "
                        f"proceeds to clear item.file_url/archive_status and commit, exactly as if the "
                        f"delete had succeeded; the False return propagates nowhere, so "
                        f"_compensate_failed_attempt's `ok` stays True and _resolve_cleanup_marker "
                        f"(...,ok=True) clears the marker"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_g():
                section("G — Superseded-attempt archive-key race (production-derived identities)")

                class _FakeS3StoreG:
                    """In-memory fake S3 object map — no network. Records only
                    what production actually supplies: the exact key/payload
                    given to an upload, and a full history of delete attempts."""
                    def __init__(self):
                        self.objects = {}       # key -> {"payload": ...}
                        self.delete_calls = []  # every key a delete was attempted against, in order
                        self.upload_calls = []  # [{"key":..., "payload":...}], in call order

                    def record_upload(self, key, payload):
                        self.objects[key] = {"payload": payload}
                        self.upload_calls.append({"key": key, "payload": payload})
                        return key

                    def delete_file(self, key):
                        self.delete_calls.append(key)
                        if key in self.objects:
                            del self.objects[key]
                            return True
                        return False

                store_g = _FakeS3StoreG()

                reached_embed_g = threading.Event()
                release_embed_g = threading.Event()
                reached_delete_g = threading.Event()
                release_delete_g = threading.Event()
                worker_exc_g = []
                embed_call_count_g = {"n": 0}

                def _phased_get_embeddings_g(texts):
                    embed_call_count_g["n"] += 1
                    if embed_call_count_g["n"] == 1:
                        # Attempt A's own call — block genuinely inside the
                        # real pre-batch phase, proving A's worker is
                        # authentically parked (not finished) with its own
                        # archive already committed. Every later call
                        # (attempt B's) completes immediately — B only needs
                        # to reach completion so its own real key can be
                        # observed, not to be paused.
                        reached_embed_g.set()
                        release_embed_g.wait(timeout=10)
                    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

                class _FakeIndexG:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        pass

                def _fake_embedding_service_init_g(self):
                    self.pinecone_available = True
                    self.index = _FakeIndexG()

                class _GatedS3Service:
                    """Mirrors the real S3Service's upload_file/delete_file
                    signatures. upload_file completes immediately and records
                    the EXACT key/payload production supplied — this test
                    never chooses it. delete_file blocks (reached/release) so
                    a compensation call can be paused genuinely AFTER its
                    ownership check but BEFORE the delete takes effect —
                    there is no other real hook point without editing
                    production code."""
                    def __init__(self):
                        pass

                    def upload_file(self, file_content, filename, content_type):
                        return store_g.record_upload(filename, file_content)

                    def delete_file(self, key):
                        reached_delete_g.set()
                        release_delete_g.wait(timeout=10)
                        return store_g.delete_file(key)

                mkuser("repro_g_u", successful_sources_total=0)
                mkitem("repro_g_item", "repro_g_u", processed=False)

                def _run_pdf_worker_a():
                    try:
                        library_router.process_pdf_embeddings(
                            "repro_g_item", b"%PDF-fake-bytes-A", "repro_g_u")
                    except Exception as e:
                        worker_exc_g.append(e)

                t_g_a = None
                t_g_comp = None
                tok_g_a = None
                tok_g_b = None
                key_a = None
                key_b = None

                with mock.patch("app.routers.library.S3Service", _GatedS3Service), \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_g), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                     mock.patch.object(es_mod, "_get_embeddings", side_effect=_phased_get_embeddings_g), \
                     mock.patch("app.services.text_extract.pdf_to_structured_text",
                                 return_value="attempt A's real extracted PDF text, long enough to index"):

                    t_g_a = threading.Thread(target=_run_pdf_worker_a)
                    t_g_a.start()
                    try:
                        got_reached_embed = reached_embed_g.wait(timeout=10)
                        observe(
                            "attempt A's worker genuinely reached the pre-batch embedding phase, "
                            "after its own real archive upload already committed",
                            got_reached_embed,
                        )
                        if not got_reached_embed:
                            raise RuntimeError("attempt A's worker never reached _get_embeddings")

                        item_after_archive = refresh_item("repro_g_item")
                        tok_g_a = item_after_archive.last_processing_attempt_id
                        key_a_from_row = item_after_archive.file_url
                        key_a = find_artifact_key(
                            "repro_g_item", "repro_g_u", tok_g_a, artifact_kind="s3",
                            observed_upload=(store_g.upload_calls[-1] if store_g.upload_calls else None))
                        observe("attempt A token, observed while A's worker is still the current owner", tok_g_a)
                        observe("attempt A's archive key, observed from the item row (file_url)", key_a_from_row)
                        observe("attempt A's archive key, via find_artifact_key()", key_a)
                        if not tok_g_a or not key_a or key_a != key_a_from_row:
                            raise RuntimeError(
                                f"attempt A's real archive path did not produce a consistent, "
                                f"observable key — row={key_a_from_row!r} adapter={key_a!r}")

                        def _run_compensation_a():
                            try:
                                library_router._compensate_failed_attempt(
                                    "repro_g_item", "repro_g_u", attempt_token=tok_g_a, s3_key=key_a,
                                    reason="section G repro — attempt A stale compensation")
                            except Exception as e:
                                worker_exc_g.append(e)

                        t_g_comp = threading.Thread(target=_run_compensation_a)
                        t_g_comp.start()
                        try:
                            got_reached_delete = reached_delete_g.wait(timeout=10)
                            observe(
                                "attempt A's compensation genuinely reached S3Service.delete_file(), "
                                "after passing its own ownership check",
                                got_reached_delete,
                            )
                            if not got_reached_delete:
                                raise RuntimeError("attempt A's compensation never reached delete_file()")

                            # While A's compensation is paused right there,
                            # attempt B supersedes A for real, then goes
                            # through the SAME production archive path — this
                            # test never chooses or constructs B's key.
                            db.query(LibraryItem).filter(LibraryItem.id == "repro_g_item").update(
                                {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                            db.commit()
                            real_reap("repro_g_u", "repro_g_probe1")
                            if refresh_item("repro_g_item").entitlement_status != "released":
                                raise RuntimeError("expected the real reaper to release repro_g_item")

                            library_router.process_pdf_embeddings(
                                "repro_g_item", b"%PDF-fake-bytes-B", "repro_g_u")

                            item_after_b = refresh_item("repro_g_item")
                            tok_g_b = item_after_b.last_processing_attempt_id
                            key_b_from_row = item_after_b.file_url
                            key_b = find_artifact_key(
                                "repro_g_item", "repro_g_u", tok_g_b, artifact_kind="s3",
                                observed_upload=(store_g.upload_calls[-1] if store_g.upload_calls else None))
                            observe("attempt B token (production-derived, must differ from A)", tok_g_b)
                            observe("attempt B's archive key, observed from the item row (file_url)", key_b_from_row)
                            observe("attempt B's archive key, via find_artifact_key()", key_b)
                            if not tok_g_b or tok_g_b == tok_g_a or not key_b or key_b != key_b_from_row:
                                raise RuntimeError(
                                    f"attempt B's real archive path did not produce a consistent, "
                                    f"observable, genuinely new attempt/key — tok_b={tok_g_b!r} "
                                    f"tok_a={tok_g_a!r} row={key_b_from_row!r} adapter={key_b!r}")
                        finally:
                            # Always release A's paused compensation, even if
                            # a setup assertion above raised, so its thread
                            # can never be left stranded.
                            release_delete_g.set()
                        t_g_comp.join(timeout=10)
                    finally:
                        # Always release attempt A's worker too — it is
                        # STILL genuinely blocked inside _get_embeddings this
                        # entire time.
                        release_embed_g.set()
                    t_g_a.join(timeout=10)

                if worker_exc_g:
                    raise RuntimeError(f"a section G thread raised {worker_exc_g[0]!r}")
                if t_g_a is None or t_g_a.is_alive():
                    raise RuntimeError("attempt A's worker thread did not finish within its join timeout")
                if t_g_comp is None or t_g_comp.is_alive():
                    raise RuntimeError("attempt A's compensation thread did not finish within its join timeout")
                if not key_a or not key_b or not tok_g_b:
                    raise RuntimeError("setup incomplete — key_a/key_b/tok_g_b were not established")

                final_item_g = refresh_item("repro_g_item")
                key_b_object_survives = key_b in store_g.objects
                keys_are_fixed_design = (key_a == key_b)
                observe("key A vs key B — production's own behavior decides fixed-key vs "
                        "attempt-scoped-key design", {"key_a": key_a, "key_b": key_b,
                                                       "fixed_key_design": keys_are_fixed_design})
                observe("delete_file() call history", store_g.delete_calls)
                observe("live S3 objects remaining in the fake store", list(store_g.objects.keys()))
                observe("item file_url / archive_status after A's stale compensation ran",
                        (final_item_g.file_url, final_item_g.archive_status))
                observe("item cleanup_state / cleanup_detail after A's stale compensation ran",
                        (final_item_g.cleanup_state, final_item_g.cleanup_detail))

                # --- G.A: object safety — valid under EITHER a fixed-key or
                # an attempt-scoped-key production design; never requires
                # key_a == key_b or key_a != key_b. ---------------------------
                check(
                    "[object safety] a stale, superseded attempt A cannot delete attempt B's live "
                    "archive object, regardless of whether production uses a fixed or an "
                    "attempt-scoped key",
                    key_b_object_survives,
                    detail=(
                        f"key_a={key_a!r} key_b={key_b!r} delete_calls={store_g.delete_calls} "
                        f"remaining_objects={list(store_g.objects.keys())} — "
                        + (
                            "production currently uses a FIXED key (key_a==key_b), so A's single "
                            "delete of that key removed B's live object"
                            if keys_are_fixed_design else
                            "production currently uses an ATTEMPT-SCOPED key (key_a!=key_b), but A "
                            "still deleted the object at key_b"
                        )
                        + " — _cleanup_archive_after_abandoned_processing's ownership check is "
                          "read-then-act with no atomic claim on the delete that follows"
                    ),
                )
                check(
                    "[object safety] attempt B's file_url points at B's own key and its "
                    "archive_status is intact, unmutated by attempt A's stale compensation",
                    final_item_g.file_url == key_b and final_item_g.archive_status == "stored",
                    detail=(
                        f"file_url={final_item_g.file_url!r} archive_status={final_item_g.archive_status!r} "
                        f"expected_key_b={key_b!r}"
                    ),
                )
                check(
                    "[object safety] attempt B's durable attempt identity "
                    "(last_processing_attempt_id) is untouched by attempt A's archive cleanup",
                    final_item_g.last_processing_attempt_id == tok_g_b,
                    detail=(
                        f"last_processing_attempt_id={final_item_g.last_processing_attempt_id!r} "
                        f"expected={tok_g_b!r}"
                    ),
                )

                # --- G.B: cleanup-accountability, via the implementation- ----
                # neutral adapter — deliberately separate from object safety
                # above. Does NOT require after_g.cleanup_state=='failed' on
                # B's row; accepts either a correctly-owned item marker or a
                # separate cleanup ledger, exactly like Section B. Under an
                # attempt-scoped-key design where nothing was actually
                # damaged, no accountability record is required either.
                rec_g_a = find_cleanup_record("repro_g_item", "repro_g_u", tok_g_a, "s3", artifact_key=key_a)
                observe(
                    f"cleanup record found for attempt A (kind=s3, key={key_a!r}, via find_cleanup_record)",
                    rec_g_a,
                )
                accountability_holds = (
                    key_b_object_survives
                    or (rec_g_a is not None and rec_g_a.get("state") in ("pending", "failed"))
                )
                check(
                    "[cleanup-accountability] if attempt A's compensation was refused, failed, or "
                    "caused damage, durable cleanup work exists for attempt A / artifact kind s3 / "
                    "A's own key — as EITHER a correctly-owned item marker OR a separate cleanup "
                    "ledger, never required on attempt B's row",
                    accountability_holds,
                    detail=(
                        f"find_cleanup_record(...)={rec_g_a!r} — a successful (non-raising) DELETE "
                        f"of the WRONG object is not an exception, so `ok` stays True and "
                        f"_resolve_cleanup_marker clears the marker as if nothing happened; current "
                        f"code also has no separate cleanup-ledger model, so no record exists under "
                        f"either representation this adapter accepts"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_h():
                section("H1 — EPUB full-attempt lease coverage before the first embedding batch")

                class _FakeIndexH1:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        pass

                def _fake_embedding_service_init_h1(self):
                    self.pinecone_available = True
                    self.index = _FakeIndexH1()

                reached_h1 = threading.Event()
                release_h1 = threading.Event()
                worker_exc_h1 = []

                def _blocking_get_embeddings_h1(texts):
                    reached_h1.set()
                    release_h1.wait(timeout=10)
                    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

                mkuser("repro_h1_u", successful_sources_total=0)
                mkitem("repro_h1_item", "repro_h1_u", processed=False)

                def _run_worker_h1():
                    try:
                        library_router.process_epub_embeddings(
                            "repro_h1_item", b"fake-epub-bytes", "repro_h1_u")
                    except Exception as e:
                        worker_exc_h1.append(e)

                with mock.patch("app.routers.library.S3Service") as MockS3_h1, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_h1), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                     mock.patch.object(es_mod, "_get_embeddings", side_effect=_blocking_get_embeddings_h1), \
                     mock.patch("app.routers.library._extract_epub_text",
                                 return_value="some real extracted EPUB text, long enough to index"):
                    MockS3_h1.return_value.upload_file.return_value = "repro_h1_u/repro_h1_item.epub"
                    MockS3_h1.return_value.delete_file.return_value = True
                    t_h1 = threading.Thread(target=_run_worker_h1)
                    t_h1.start()
                    tok_h1 = None
                    try:
                        got_reached_h1 = reached_h1.wait(timeout=10)
                        observe("worker (EPUB) genuinely entered _get_embeddings (pre-batch phase)", got_reached_h1)
                        if not got_reached_h1:
                            raise RuntimeError("EPUB worker never reached _get_embeddings")

                        item_mid_h1 = refresh_item("repro_h1_item")
                        tok_h1 = item_mid_h1.reservation_lease_token
                        observe("attempt token (H1, EPUB), captured while the worker is still the "
                                "current owner", tok_h1)
                        if not tok_h1:
                            raise RuntimeError("reservation fixture did not produce a token")

                        forced_stale_expiry_h1 = NOW - datetime.timedelta(seconds=1)
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_h1_item").update(
                            {"reservation_lease_expires_at": forced_stale_expiry_h1})
                        db.commit()
                        observe("lease expiry forced into an expired state, worker still blocked (EPUB)",
                                forced_stale_expiry_h1)

                        heartbeat_observed_h1, snapshot_h1 = _poll_for_heartbeat_renewal(
                            "repro_h1_item", "repro_h1_u", tok_h1, forced_stale_expiry_h1)
                        observe("heartbeat observed before the reaper ran (EPUB)", heartbeat_observed_h1)

                        real_reap("repro_h1_u", "repro_h1_probe1")
                        status_during_block_h1 = refresh_item("repro_h1_item").entitlement_status
                        observe("entitlement_status WHILE the EPUB worker is still blocked in "
                                "_get_embeddings, after the reaper ran", status_during_block_h1)
                    finally:
                        release_h1.set()
                    t_h1.join(timeout=10)

                if worker_exc_h1:
                    raise RuntimeError(f"H1 (EPUB) worker thread raised {worker_exc_h1[0]!r}")
                if t_h1.is_alive():
                    raise RuntimeError("H1 (EPUB) worker thread did not finish within its join timeout")

                after_h1 = refresh_item("repro_h1_item")
                observe("final entitlement_status / processed / content (H1, EPUB)",
                        (after_h1.entitlement_status, after_h1.processed, after_h1.content))

                check(
                    "(EPUB) an active worker's production heartbeat renews the lease during the "
                    "long pre-batch provider phase, before the reaper ever runs",
                    heartbeat_observed_h1,
                    detail=(
                        f"heartbeat_observed={heartbeat_observed_h1!r} — process_epub_embeddings "
                        f"wires on_batch the same way process_pdf_embeddings does, so it has the "
                        f"identical pre-first-batch gap already proven in Section D"
                    ),
                )
                check(
                    "(EPUB) the reservation survives the reaper while the worker is genuinely "
                    "still blocked in the pre-batch provider phase",
                    status_during_block_h1 == "pending",
                    detail=f"status_during_block={status_during_block_h1!r}",
                )
                check(
                    "(EPUB) the worker finalizes only if it retained authentic ownership — a "
                    "reaped worker's own eventual finalize never converts the reservation into "
                    "'consumed'",
                    not (after_h1.entitlement_status == "consumed" and not heartbeat_observed_h1),
                    detail=(
                        f"entitlement_status={after_h1.entitlement_status!r} "
                        f"heartbeat_observed={heartbeat_observed_h1!r}"
                    ),
                )

                # ═════════════════════════════════════════════════════════
                section("H2 — URL full-attempt lease coverage during the real fetch/extraction phase")

                class _FakeIndexH2:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        pass

                def _fake_embedding_service_init_h2(self):
                    self.pinecone_available = True
                    self.index = _FakeIndexH2()

                class _FakeUrlResponse:
                    def __init__(self, text):
                        self.text = text

                reached_h2 = threading.Event()
                release_h2 = threading.Event()
                worker_exc_h2 = []
                fetch_observation_h2 = {}

                def _blocking_fetch_h2(url, headers=None, timeout=None):
                    # Records the REAL durable state at the moment the fetch
                    # begins, via a genuinely separate session — proves (or
                    # disproves) "reservation occurred before the fetch"
                    # from outside the worker's own session/identity map.
                    s2 = SessionLocal()
                    try:
                        row = s2.query(LibraryItem).filter(LibraryItem.id == "repro_h2_item").first()
                        fetch_observation_h2["entitlement_status"] = row.entitlement_status if row else None
                        fetch_observation_h2["has_attempt_token"] = bool(row.reservation_lease_token) if row else False
                    finally:
                        s2.close()
                    reached_h2.set()
                    release_h2.wait(timeout=10)
                    return _FakeUrlResponse(
                        "<html><body><article>some real extracted URL text, long enough to "
                        "index, repeated to remain safely non-empty after boilerplate "
                        "stripping</article></body></html>"
                    )

                mkuser("repro_h2_u", successful_sources_total=0)
                mkitem("repro_h2_item", "repro_h2_u", processed=False, title="https://example.invalid/article")

                def _run_worker_h2():
                    try:
                        library_router.process_url_embeddings(
                            "repro_h2_item", "https://example.invalid/article", "repro_h2_u")
                    except Exception as e:
                        worker_exc_h2.append(e)

                with mock.patch("app.routers.library.fetch_public_url", side_effect=_blocking_fetch_h2), \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_h2), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]] * 2):
                    t_h2 = threading.Thread(target=_run_worker_h2)
                    t_h2.start()
                    tok_h2 = None
                    try:
                        got_reached_h2 = reached_h2.wait(timeout=10)
                        observe("worker (URL) genuinely entered the real fetch_public_url call", got_reached_h2)
                        if not got_reached_h2:
                            raise RuntimeError("URL worker never reached fetch_public_url")

                        item_mid_h2 = refresh_item("repro_h2_item")
                        tok_h2 = item_mid_h2.reservation_lease_token
                        observe("attempt token (H2, URL), captured while the worker is still the "
                                "current owner", tok_h2)
                        if not tok_h2:
                            raise RuntimeError("reservation fixture did not produce a token")

                        forced_stale_expiry_h2 = NOW - datetime.timedelta(seconds=1)
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_h2_item").update(
                            {"reservation_lease_expires_at": forced_stale_expiry_h2})
                        db.commit()
                        observe("lease expiry forced into an expired state, worker still blocked (URL)",
                                forced_stale_expiry_h2)

                        heartbeat_observed_h2, snapshot_h2 = _poll_for_heartbeat_renewal(
                            "repro_h2_item", "repro_h2_u", tok_h2, forced_stale_expiry_h2)
                        observe("heartbeat observed before the reaper ran (URL)", heartbeat_observed_h2)

                        real_reap("repro_h2_u", "repro_h2_probe1")
                        status_during_block_h2 = refresh_item("repro_h2_item").entitlement_status
                        observe("entitlement_status WHILE the URL worker is still blocked in "
                                "fetch_public_url, after the reaper ran", status_during_block_h2)
                    finally:
                        release_h2.set()
                    t_h2.join(timeout=10)

                if worker_exc_h2:
                    raise RuntimeError(f"H2 (URL) worker thread raised {worker_exc_h2[0]!r}")
                if t_h2.is_alive():
                    raise RuntimeError("H2 (URL) worker thread did not finish within its join timeout")

                after_h2 = refresh_item("repro_h2_item")
                observe("final entitlement_status / processed / content (H2, URL)",
                        (after_h2.entitlement_status, after_h2.processed, after_h2.content))
                observe("fetch-time observation (must show reservation already existed)", fetch_observation_h2)

                check(
                    "(URL) reservation occurred before the mocked URL fetch began",
                    fetch_observation_h2.get("entitlement_status") == "pending"
                    and fetch_observation_h2.get("has_attempt_token") is True,
                    detail=f"observed_at_fetch={fetch_observation_h2!r}",
                )
                check(
                    "(URL) an active worker's production heartbeat renews the lease during the "
                    "real fetch/extraction phase, before the reaper ever runs",
                    heartbeat_observed_h2,
                    detail=(
                        f"heartbeat_observed={heartbeat_observed_h2!r} — the fetch/extraction "
                        f"phase runs entirely before index_text()'s on_batch heartbeat is ever "
                        f"wired, so it has zero coverage, even more clearly than the pre-first-"
                        f"batch gap already proven in Section D"
                    ),
                )
                check(
                    "(URL) the real reaper preserves the live attempt while the worker is "
                    "genuinely still blocked in the fetch/extraction phase",
                    status_during_block_h2 == "pending",
                    detail=f"status_during_block={status_during_block_h2!r}",
                )
                check(
                    "(URL) a GENUINELY heartbeat-protected attempt (this exact worker — never "
                    "reaped, never superseded) legitimately finalizes successfully once released "
                    "— heartbeat protection and successful finalization are the SAME real outcome, "
                    "not a contradiction (Follow-up 2B correction of the original H2's 4th check, "
                    "which required BOTH 'heartbeat renews' AND 'stale completion cannot finalize' "
                    "for the identical, never-superseded worker — logically unsatisfiable once "
                    "heartbeat protection genuinely works)",
                    after_h2.entitlement_status == "consumed" and after_h2.content is not None,
                    detail=(
                        f"entitlement_status={after_h2.entitlement_status!r} "
                        f"content={after_h2.content!r} heartbeat_observed={heartbeat_observed_h2!r}"
                    ),
                )

                # ═════════════════════════════════════════════════════════
                section("H2-STALE — URL: a GENUINELY superseded attempt (reaped, then a real "
                        "attempt B reserved) cannot mutate title/content/status/error/progress/"
                        "finalization once released, and starts no later external call")

                class _FakeIndexH2s:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        pass

                def _fake_embedding_service_init_h2s(self):
                    self.pinecone_available = True
                    self.index = _FakeIndexH2s()

                reached_h2s = threading.Event()
                release_h2s = threading.Event()
                worker_exc_h2s = []
                later_call_began_h2s = threading.Event()

                def _blocking_fetch_h2s(url, headers=None, timeout=None):
                    reached_h2s.set()
                    release_h2s.wait(timeout=10)
                    return _FakeUrlResponse(
                        "<html><head><title>Attempt A's stale page title</title></head><body>"
                        "<article>attempt A's real fetched text, long enough to index, "
                        "repeated to remain safely non-empty after boilerplate stripping"
                        "</article></body></html>"
                    )

                def _blocking_get_embeddings_h2s(texts):
                    # Proves attempt A never even reaches embedding generation
                    # (a LATER external call) once it has been genuinely
                    # superseded — see check below.
                    later_call_began_h2s.set()
                    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

                mkuser("repro_h2s_u", successful_sources_total=0)
                mkitem("repro_h2s_item", "repro_h2s_u", processed=False,
                       title="https://example.invalid/stale-article")

                def _run_worker_h2s():
                    try:
                        library_router.process_url_embeddings(
                            "repro_h2s_item", "https://example.invalid/stale-article", "repro_h2s_u")
                    except Exception as e:
                        worker_exc_h2s.append(e)

                with mock.patch("app.routers.library.fetch_public_url", side_effect=_blocking_fetch_h2s), \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_h2s), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                     mock.patch.object(es_mod, "_get_embeddings", side_effect=_blocking_get_embeddings_h2s), \
                     _deterministic_stale_supersession() as supersede_h2s:
                    t_h2s = threading.Thread(target=_run_worker_h2s)
                    t_h2s.start()
                    tok_h2s_a = None
                    tok_h2s_b = None
                    b_title_h2s = "attempt B's own real title, must survive attempt A's later fetch"
                    try:
                        got_reached_h2s = reached_h2s.wait(timeout=10)
                        observe("worker (URL, stale case) genuinely entered the real "
                                "fetch_public_url call", got_reached_h2s)
                        if not got_reached_h2s:
                            raise RuntimeError("URL worker never reached fetch_public_url")

                        item_mid_h2s = refresh_item("repro_h2s_item")
                        tok_h2s_a = item_mid_h2s.reservation_lease_token
                        observe("attempt A token (H2-stale), captured while A is still the "
                                "current owner", tok_h2s_a)
                        if not tok_h2s_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Deterministic supersession (2B-R1 correction #10):
                        # A's background heartbeat is guaranteed inert for
                        # the whole `with` block above, so this sequence
                        # cannot race a real renewal — the outcome is
                        # certain, not merely likely.
                        tok_h2s_b = supersede_h2s("repro_h2s_item", "repro_h2s_u", "repro_h2s_probe1")
                        observe("attempt B token (H2-stale, must differ from A)", tok_h2s_b)
                        if tok_h2s_b == tok_h2s_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")

                        db.query(LibraryItem).filter(LibraryItem.id == "repro_h2s_item").update(
                            {"title": b_title_h2s})
                        db.commit()
                    finally:
                        release_h2s.set()
                    t_h2s.join(timeout=10)

                if worker_exc_h2s:
                    raise RuntimeError(f"H2-stale worker thread raised {worker_exc_h2s[0]!r}")
                if t_h2s.is_alive():
                    raise RuntimeError("H2-stale worker thread did not finish within its join timeout")
                if not tok_h2s_a or not tok_h2s_b:
                    raise RuntimeError("setup incomplete — tok_h2s_a/tok_h2s_b were not established")

                after_h2s = refresh_item("repro_h2s_item")
                observe("final title/content/entitlement_status/reservation_lease_token (H2-stale)",
                        (after_h2s.title, after_h2s.content, after_h2s.entitlement_status,
                         after_h2s.reservation_lease_token))
                observe("did attempt A's stale worker begin a LATER external call "
                        "(embedding generation) after being superseded?", later_call_began_h2s.is_set())

                check(
                    "(URL, stale) a genuinely superseded attempt A never begins a LATER external "
                    "call (embedding generation) once ownership is gone",
                    not later_call_began_h2s.is_set(),
                    detail=(
                        f"later_call_began={later_call_began_h2s.is_set()!r} — attempt A's own "
                        f"in-process check_ownership()/guard.check() calls are the only thing that "
                        f"can stop it between the fetch returning and embedding generation starting"
                    ),
                )
                check(
                    "(URL, stale) attempt B's own title/reservation identity are untouched by "
                    "attempt A's later, stale completion",
                    after_h2s.title == b_title_h2s and after_h2s.reservation_lease_token == tok_h2s_b,
                    detail=(
                        f"title={after_h2s.title!r} expected={b_title_h2s!r} "
                        f"reservation_lease_token={after_h2s.reservation_lease_token!r} expected={tok_h2s_b!r}"
                    ),
                )
                check(
                    "(URL, stale) attempt A's stale completion never finalizes as though it still "
                    "owned the reservation",
                    after_h2s.entitlement_status != "consumed",
                    detail=f"entitlement_status={after_h2s.entitlement_status!r}",
                )

                # ═════════════════════════════════════════════════════════
                section("H3 — OCR full lifecycle: reservation, S3 download, and heartbeat before "
                        "the first checkpoint")

                class _FakeIndexH3:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        pass

                def _fake_embedding_service_init_h3(self):
                    self.pinecone_available = True
                    self.index = _FakeIndexH3()

                reached_ocr_h3 = threading.Event()
                release_ocr_h3 = threading.Event()
                worker_exc_h3 = []
                s3_download_observation_h3 = {}

                class _ObservingS3ServiceH3:
                    """Mirrors the real S3Service's download_file signature.
                    Records the REAL durable state (via a genuinely separate
                    session) at the moment the download begins — proves (or
                    disproves) "reservation exists before the archive
                    download begins" — then returns immediately; the OCR
                    call itself, not the download, is where H3 blocks."""
                    def __init__(self):
                        pass

                    def download_file(self, ref):
                        s2 = SessionLocal()
                        try:
                            row = s2.query(LibraryItem).filter(LibraryItem.id == "repro_h3_item").first()
                            s3_download_observation_h3["entitlement_status"] = row.entitlement_status if row else None
                            s3_download_observation_h3["has_attempt_token"] = bool(row.reservation_lease_token) if row else False
                        finally:
                            s2.close()
                        return b"fake-pdf-bytes-for-ocr-h3"

                def _ocr_side_effect_h3(pdf_bytes, on_progress=None):
                    # Blocks BEFORE any progress checkpoint — simulating a
                    # long first page/checkpoint, exactly the same
                    # pre-first-checkpoint shape Section D already proved
                    # for the embedding pipelines' on_batch heartbeat.
                    reached_ocr_h3.set()
                    release_ocr_h3.wait(timeout=10)
                    return "OCR'd fake text, long enough to index for section H3"

                mkuser("repro_h3_u", successful_sources_total=0)
                mkitem("repro_h3_item", "repro_h3_u", processed=False, file_url="s3://fake/repro_h3_item.pdf")

                def _run_worker_h3():
                    try:
                        _call_with_attempt_token_if_supported(
                            library_router._run_ocr, None, "repro_h3_item", "repro_h3_u")
                    except Exception as e:
                        worker_exc_h3.append(e)

                with mock.patch("app.routers.library.S3Service", _ObservingS3ServiceH3), \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_h3), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]]), \
                     mock.patch("app.services.ocr_service.ocr_pdf", side_effect=_ocr_side_effect_h3):
                    t_h3 = threading.Thread(target=_run_worker_h3)
                    t_h3.start()
                    tok_h3 = None
                    try:
                        got_reached_h3 = reached_ocr_h3.wait(timeout=10)
                        observe("worker genuinely reached the OCR provider call, after "
                                "reservation + S3 download", got_reached_h3)
                        if not got_reached_h3:
                            raise RuntimeError("worker never reached ocr_service.ocr_pdf")

                        item_mid_h3 = refresh_item("repro_h3_item")
                        tok_h3 = item_mid_h3.reservation_lease_token
                        observe("attempt token (H3, OCR), captured while the worker is still the "
                                "current owner", tok_h3)
                        if not tok_h3:
                            raise RuntimeError("reservation fixture did not produce a token")

                        forced_stale_expiry_h3 = NOW - datetime.timedelta(seconds=1)
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_h3_item").update(
                            {"reservation_lease_expires_at": forced_stale_expiry_h3})
                        db.commit()
                        observe("lease expiry forced into an expired state, worker still blocked (OCR)",
                                forced_stale_expiry_h3)

                        heartbeat_observed_h3, snapshot_h3 = _poll_for_heartbeat_renewal(
                            "repro_h3_item", "repro_h3_u", tok_h3, forced_stale_expiry_h3)
                        observe("heartbeat observed before the reaper ran (OCR, before its first "
                                "page checkpoint)", heartbeat_observed_h3)

                        real_reap("repro_h3_u", "repro_h3_probe1")
                        status_during_block_h3 = refresh_item("repro_h3_item").entitlement_status
                        observe("entitlement_status WHILE the worker is still blocked in OCR, "
                                "after the reaper ran", status_during_block_h3)
                    finally:
                        release_ocr_h3.set()
                    t_h3.join(timeout=10)

                if worker_exc_h3:
                    raise RuntimeError(f"H3 (OCR) worker thread raised {worker_exc_h3[0]!r}")
                if t_h3.is_alive():
                    raise RuntimeError("H3 (OCR) worker thread did not finish within its join timeout")

                after_h3 = refresh_item("repro_h3_item")
                observe("final ocr_status / processing_error / entitlement_status (H3, OCR)",
                        (after_h3.ocr_status, after_h3.processing_error, after_h3.entitlement_status))

                check(
                    "(OCR) reservation exists (pending status + a real attempt token) before the "
                    "archive download begins",
                    s3_download_observation_h3.get("entitlement_status") == "pending"
                    and s3_download_observation_h3.get("has_attempt_token") is True,
                    detail=f"observed_at_download={s3_download_observation_h3!r}",
                )
                check(
                    "(OCR) an active worker's production heartbeat renews the lease before the "
                    "reaper runs, even before its first page-progress checkpoint",
                    heartbeat_observed_h3,
                    detail=(
                        f"heartbeat_observed={heartbeat_observed_h3!r} snapshot={snapshot_h3!r} — "
                        f"OCR's own progress() callback DOES correctly check "
                        f"renew_reservation_lease()'s return value and aborts on ownership loss, "
                        f"but it is only ever called AFTER at least one page finishes; nothing "
                        f"renews the lease during the FIRST page/checkpoint, the same pre-first-"
                        f"checkpoint gap already proven for the embedding pipelines in Section D"
                    ),
                )
                check(
                    "(OCR) the reservation survives the reaper while the worker is genuinely "
                    "still blocked before its first checkpoint",
                    status_during_block_h3 == "pending",
                    detail=f"status_during_block={status_during_block_h3!r}",
                )
                check(
                    "(OCR) a GENUINELY heartbeat-protected attempt (this exact worker — never "
                    "reaped, never superseded) legitimately finalizes successfully once released "
                    "— heartbeat protection and successful finalization are the SAME real outcome "
                    "(Follow-up 2B correction of the original H3's 4th check, which required BOTH "
                    "'heartbeat renews' AND 'stale completion cannot successfully finalize' for the "
                    "identical, never-superseded worker — logically unsatisfiable once heartbeat "
                    "protection genuinely works)",
                    after_h3.ocr_status == "done" and after_h3.entitlement_status == "consumed",
                    detail=(
                        f"ocr_status={after_h3.ocr_status!r} entitlement_status={after_h3.entitlement_status!r} "
                        f"heartbeat_observed={heartbeat_observed_h3!r}"
                    ),
                )

                # ═════════════════════════════════════════════════════════
                section("H3-STALE — OCR: a GENUINELY superseded attempt (reaped, then a real "
                        "attempt B reserved) cannot commit ocr_status/progress/content/error once "
                        "released, and starts no later external call")

                reached_ocr_h3s = threading.Event()
                release_ocr_h3s = threading.Event()
                worker_exc_h3s = []
                later_call_began_h3s = threading.Event()

                def _ocr_side_effect_h3s(pdf_bytes, on_progress=None):
                    reached_ocr_h3s.set()
                    release_ocr_h3s.wait(timeout=10)
                    return "OCR'd fake text from attempt A, stale, must not be committed"

                def _fake_embedding_service_init_h3s(self):
                    later_call_began_h3s.set()
                    self.pinecone_available = True

                    class _NoOpIndex:
                        def upsert(self, vectors, namespace):
                            pass

                        def delete(self, filter, namespace=None):
                            pass
                    self.index = _NoOpIndex()

                mkuser("repro_h3s_u", successful_sources_total=0)
                mkitem("repro_h3s_item", "repro_h3s_u", processed=False,
                       file_url="s3://fake/repro_h3s_item.pdf")

                def _run_worker_h3s():
                    try:
                        # Same token-signature adapter H3 (live) uses —
                        # correction #10: future-compatible if `_run_ocr`
                        # ever grows an `attempt_token` parameter, an
                        # identical call shape to the live case above.
                        _call_with_attempt_token_if_supported(
                            library_router._run_ocr, None, "repro_h3s_item", "repro_h3s_u")
                    except Exception as e:
                        worker_exc_h3s.append(e)

                with mock.patch("app.routers.library.S3Service") as MockS3_h3s, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_h3s), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]]), \
                     mock.patch("app.services.ocr_service.ocr_pdf", side_effect=_ocr_side_effect_h3s), \
                     _deterministic_stale_supersession() as supersede_h3s:
                    MockS3_h3s.return_value.download_file.return_value = b"fake-pdf-bytes-h3s"
                    t_h3s = threading.Thread(target=_run_worker_h3s)
                    t_h3s.start()
                    tok_h3s_a = None
                    tok_h3s_b = None
                    b_content_h3s = "attempt B's own real content, must survive attempt A's later OCR"
                    try:
                        got_reached_h3s = reached_ocr_h3s.wait(timeout=10)
                        observe("worker (OCR, stale case) genuinely reached the OCR provider call",
                                got_reached_h3s)
                        if not got_reached_h3s:
                            raise RuntimeError("worker never reached ocr_service.ocr_pdf")

                        item_mid_h3s = refresh_item("repro_h3s_item")
                        tok_h3s_a = item_mid_h3s.reservation_lease_token
                        observe("attempt A token (H3-stale), captured while A is still the "
                                "current owner", tok_h3s_a)
                        if not tok_h3s_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Deterministic supersession (correction #10).
                        tok_h3s_b = supersede_h3s("repro_h3s_item", "repro_h3s_u", "repro_h3s_probe1")
                        observe("attempt B token (H3-stale, must differ from A)", tok_h3s_b)
                        if tok_h3s_b == tok_h3s_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")

                        db.query(LibraryItem).filter(LibraryItem.id == "repro_h3s_item").update(
                            {"content": b_content_h3s, "ocr_status": "running"})
                        db.commit()
                    finally:
                        release_ocr_h3s.set()
                    t_h3s.join(timeout=10)

                if worker_exc_h3s:
                    raise RuntimeError(f"H3-stale worker thread raised {worker_exc_h3s[0]!r}")
                if t_h3s.is_alive():
                    raise RuntimeError("H3-stale worker thread did not finish within its join timeout")
                if not tok_h3s_a or not tok_h3s_b:
                    raise RuntimeError("setup incomplete — tok_h3s_a/tok_h3s_b were not established")

                after_h3s = refresh_item("repro_h3s_item")
                observe("final ocr_status/content/entitlement_status (H3-stale)",
                        (after_h3s.ocr_status, after_h3s.content, after_h3s.entitlement_status))

                check(
                    "(OCR, stale) a genuinely superseded attempt A never begins a LATER external "
                    "call (embedding indexing) once ownership is gone",
                    not later_call_began_h3s.is_set(),
                    detail=f"later_call_began={later_call_began_h3s.is_set()!r}",
                )
                check(
                    "(OCR, stale) attempt B's own content/ocr_status/reservation identity are "
                    "untouched by attempt A's later, stale OCR completion",
                    after_h3s.content == b_content_h3s and after_h3s.ocr_status == "running",
                    detail=(
                        f"content={after_h3s.content!r} expected={b_content_h3s!r} "
                        f"ocr_status={after_h3s.ocr_status!r}"
                    ),
                )
                check(
                    "(OCR, stale) attempt A's stale completion never finalizes as though it still "
                    "owned the reservation",
                    after_h3s.entitlement_status != "consumed",
                    detail=f"entitlement_status={after_h3s.entitlement_status!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_i():
                section("I1 — ownership-loss heartbeat abort (real renew_reservation_lease signal)")

                class _FakeIndexI:
                    def __init__(self):
                        self.upsert_calls = []  # batch sizes, in call order
                        self.delete_calls = []  # filters, in call order — proof of a REAL provider delete

                    def upsert(self, vectors, namespace):
                        if len(self.upsert_calls) == 0:
                            # Only the FIRST batch is allowed to be "already
                            # in flight" when ownership is lost — pause here
                            # so the loss can be caused for real before this
                            # call (and everything after it) proceeds.
                            reached_upsert_i.set()
                            release_upsert_i.wait(timeout=10)
                        self.upsert_calls.append(len(vectors))

                    def delete(self, filter, namespace=None):
                        self.delete_calls.append({"filter": filter, "namespace": namespace})

                fake_index_i = _FakeIndexI()

                def _fake_embedding_service_init_i(self):
                    self.pinecone_available = True
                    self.index = fake_index_i

                n_chunks_i = 250  # > 2 upsert batches of 100, so a second/third batch is observable
                fake_chunks_i = [f"chunk {i}" for i in range(n_chunks_i)]
                fake_embeddings_i = [[0.1, 0.2, 0.3, 0.4] for _ in range(n_chunks_i)]

                reached_upsert_i = threading.Event()
                release_upsert_i = threading.Event()
                worker_exc_i = []

                mkuser("repro_i1_u", successful_sources_total=0)
                mkitem("repro_i1_item", "repro_i1_u", processed=False)

                def _run_worker_i1():
                    try:
                        library_router.process_pdf_embeddings(
                            "repro_i1_item", b"%PDF-fake-bytes-I1", "repro_i1_u")
                    except Exception as e:
                        worker_exc_i.append(e)

                with mock.patch("app.routers.library.S3Service") as MockS3_i, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_i), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=fake_chunks_i), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=fake_embeddings_i), \
                     mock.patch("app.services.text_extract.pdf_to_structured_text",
                                 return_value="attempt I's real extracted PDF text, long enough to index"), \
                     _deterministic_stale_supersession() as supersede_i1:
                    MockS3_i.return_value.upload_file.return_value = "repro_i1_u/repro_i1_item.pdf"
                    MockS3_i.return_value.delete_file.return_value = True

                    t_i1 = threading.Thread(target=_run_worker_i1)
                    t_i1.start()
                    tok_i1_a = None
                    tok_i1_b = None
                    b_content_i1 = None
                    try:
                        got_reached = reached_upsert_i.wait(timeout=10)
                        observe("attempt A's worker genuinely reached the first real upsert call "
                                "(in flight)", got_reached)
                        if not got_reached:
                            raise RuntimeError("worker never reached the first upsert call")

                        item_mid_i1 = refresh_item("repro_i1_item")
                        tok_i1_a = item_mid_i1.reservation_lease_token
                        observe("attempt A token, captured while A's worker is still the current owner",
                                tok_i1_a)
                        if not tok_i1_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Deterministic supersession (correction #10): the
                        # WORKER's own `on_batch=guard.checkpoint` call —
                        # from A's OWN thread, not the background thread —
                        # still calls the REAL `renew_reservation_lease`
                        # unmodified, so this section's actual mechanism
                        # under test (a real False return caught by the
                        # worker's own checkpoint) is untouched; only the
                        # background thread's independent renewal path is
                        # made deterministic, removing the race that
                        # otherwise existed against THIS sequence.
                        tok_i1_b = supersede_i1("repro_i1_item", "repro_i1_u", "repro_i1_probe1")
                        observe("attempt B token (must differ from A)", tok_i1_b)
                        if tok_i1_b == tok_i1_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")

                        b_content_i1 = "attempt B's own real content, must survive attempt A's later processing"
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_i1_item").update(
                            {"content": b_content_i1})
                        db.commit()
                    finally:
                        release_upsert_i.set()
                    t_i1.join(timeout=10)

                if worker_exc_i:
                    raise RuntimeError(f"attempt I1's worker thread raised {worker_exc_i[0]!r}")
                if t_i1.is_alive():
                    raise RuntimeError("attempt I1's worker thread did not finish within its join timeout")
                if not tok_i1_a or not tok_i1_b:
                    raise RuntimeError("setup incomplete — tok_i1_a/tok_i1_b were not established")

                after_i1 = refresh_item("repro_i1_item")
                observe("upsert call sizes, in order (batch 1 was already in flight when "
                        "ownership was lost)", fake_index_i.upsert_calls)
                observe("item content after attempt A's later (stale) processing finished", after_i1.content)
                observe("item entitlement_status / reservation_lease_token after everything settled",
                        (after_i1.entitlement_status, after_i1.reservation_lease_token))

                upserts_after_loss = max(0, len(fake_index_i.upsert_calls) - 1)
                check(
                    "no additional provider/external upsert call begins after the worker's own "
                    "heartbeat genuinely observes ownership loss (only the already-in-flight "
                    "batch is acceptable)",
                    upserts_after_loss == 0,
                    detail=(
                        f"upsert_calls={fake_index_i.upsert_calls!r} — process_pdf_embeddings' "
                        f"on_batch heartbeat closure calls renew_reservation_lease(...) but "
                        f"discards its return value entirely; OCR's own progress callback checks "
                        f"it and raises OcrCancelled to stop immediately, but no embedding "
                        f"pipeline does the same"
                    ),
                )
                check(
                    "no success finalization occurs for the attempt that lost ownership",
                    after_i1.entitlement_status != "consumed",
                    detail=f"entitlement_status={after_i1.entitlement_status!r}",
                )
                check(
                    "attempt B's own real content survives attempt A's later (stale) processing",
                    after_i1.content == b_content_i1,
                    detail=f"content={after_i1.content!r}",
                )

                rec_i1_a = find_cleanup_record("repro_i1_item", "repro_i1_u", tok_i1_a, "vectors")
                observe("cleanup record found for attempt A (kind=vectors) after its finalize "
                        "was rejected", rec_i1_a)
                observe("real provider delete() calls logged against the fake index (proof of "
                        "explicit provider success, if resolved)", fake_index_i.delete_calls)
                # Follow-up 2B correction: the original I1 check REQUIRED
                # state in ('pending','failed') unconditionally — but once
                # ownership-loss abort genuinely works, A's own compensation
                # runs for real and its vector delete genuinely SUCCEEDS
                # (hermetic env has no live Pinecone; `EmbeddingService.
                # delete_item_vectors` calls the fake index's own delete()
                # and returns True unconditionally once no exception is
                # raised — a REAL, observable provider call, not a
                # fabricated result), which correctly resolves the record.
                # A resolved-but-unproven record (no matching delete() call
                # logged) or a record missing/attributed to B would still be
                # wrong — both remain rejected below.
                state_i1_a = rec_i1_a.get("state") if rec_i1_a else None
                resolved_with_proof = (
                    state_i1_a == "resolved" and len(fake_index_i.delete_calls) >= 1
                )
                check(
                    "attempt-scoped cleanup work for the stale attempt is durably accounted for "
                    "attempt A's exact identity — EITHER still outstanding (pending/failed, as "
                    "EITHER a correctly-owned item marker OR a separate cleanup ledger) OR "
                    "resolved ONLY after an explicit, observed, successful provider delete call "
                    "— never missing, never attributed to attempt B, never resolved with no "
                    "matching provider call",
                    rec_i1_a is not None
                    and (state_i1_a in ("pending", "failed") or resolved_with_proof),
                    detail=(
                        f"find_cleanup_record(...)={rec_i1_a!r} "
                        f"delete_calls={fake_index_i.delete_calls!r}"
                    ),
                )

                # ═════════════════════════════════════════════════════════
                section("I2 — autonomous cleanup-retry runner")

                runner = _find_cleanup_retry_runner()
                observe("autonomous cleanup-retry runner discovered (semantic capability scan, "
                        "app.services.entitlement_service + app.routers.library)", runner)

                # --- Artifact 1: a real, durably-failed VECTOR cleanup ------
                mkuser("repro_i2_u", successful_sources_total=0)
                it_i2 = mkitem("repro_i2_item", "repro_i2_u", processed=False)
                ent.reserve_free_capacity(db, it_i2, "repro_i2_u")
                tok_i2_a = refresh_item("repro_i2_item").reservation_lease_token
                observe("attempt token (I2, vector artifact)", tok_i2_a)
                if not tok_i2_a:
                    raise RuntimeError("reservation fixture did not produce a token")

                with mock.patch("app.routers.library.EmbeddingService") as MockEmbed_i2:
                    MockEmbed_i2.return_value.delete_item_vectors.side_effect = RuntimeError(
                        "simulated Pinecone outage — first vector-cleanup attempt fails")
                    library_router._compensate_failed_attempt(
                        "repro_i2_item", "repro_i2_u", attempt_token=tok_i2_a,
                        reason="I2 vector artifact setup")

                rec_before_v = find_cleanup_record("repro_i2_item", "repro_i2_u", tok_i2_a, "vectors")
                observe("durable cleanup record for the vector artifact, BEFORE any retry", rec_before_v)

                # --- Artifact 2: a real, durably-failed S3 cleanup, a -------
                # SEPARATE item/user, so cross-item isolation is meaningful.
                mkuser("repro_i2s_u", successful_sources_total=0)
                mkitem("repro_i2s_item", "repro_i2s_u", processed=False)
                it_i2s = refresh_item("repro_i2s_item")
                ent.reserve_free_capacity(db, it_i2s, "repro_i2s_u")
                tok_i2_s = refresh_item("repro_i2s_item").reservation_lease_token
                s3_key_i2 = "repro_i2s_u/repro_i2s_item.pdf"
                db.query(LibraryItem).filter(LibraryItem.id == "repro_i2s_item").update(
                    {"file_url": s3_key_i2, "archive_status": "stored"})
                db.commit()
                observe("attempt token (I2, S3 artifact)", tok_i2_s)
                if not tok_i2_s:
                    raise RuntimeError("reservation fixture did not produce a token")

                with mock.patch("app.routers.library.S3Service") as MockS3_i2:
                    MockS3_i2.return_value.delete_file.side_effect = RuntimeError(
                        "simulated S3 outage — first archive-cleanup attempt fails")
                    library_router._compensate_failed_attempt(
                        "repro_i2s_item", "repro_i2s_u", attempt_token=tok_i2_s, s3_key=s3_key_i2,
                        reason="I2 S3 artifact setup")

                rec_before_s = find_cleanup_record(
                    "repro_i2s_item", "repro_i2s_u", tok_i2_s, "s3", artifact_key=s3_key_i2)
                observe("durable cleanup record for the S3 artifact, BEFORE any retry", rec_before_s)

                # A separate, LIVE attempt's own artifact — must remain
                # untouched by whatever the runner (if any) retries above.
                mkuser("repro_i2_live_u", successful_sources_total=0)
                live_content_i2 = "a separate live attempt's own real content"
                mkitem("repro_i2_live_item", "repro_i2_live_u", processed=False, content=live_content_i2)
                it_i2_live = refresh_item("repro_i2_live_item")
                ent.reserve_free_capacity(db, it_i2_live, "repro_i2_live_u")

                if runner is None:
                    check(
                        "a real, autonomous production entry point retries durable failed "
                        "cleanup work without requiring a direct, item-specific "
                        "_compensate_failed_attempt() call — discovered as a scheduled service, "
                        "startup/background hook, queue runner, or scan function that actually "
                        "reads cleanup_state/cleanup_detail",
                        False,
                        detail=(
                            "no such callable was found anywhere in app.services."
                            "entitlement_service or app.routers.library by semantic capability "
                            "scan (source references to cleanup_state/cleanup_detail, excluding "
                            f"the known item-specific write helpers) — vector artifact left "
                            f"durable state={rec_before_v!r}, S3 artifact left durable "
                            f"state={rec_before_s!r}; both remain permanently un-retried with no "
                            f"runner to discover them"
                        ),
                    )
                else:
                    # GREEN-capable path: a future runner exists — configure
                    # the fake providers to SUCCEED this time, invoke the
                    # real runner with NO item-specific direct compensation
                    # call, and verify it actually resolved both artifacts
                    # and touched nothing belonging to the live attempt.
                    with mock.patch("app.routers.library.EmbeddingService") as MockEmbed_ok, \
                         mock.patch("app.routers.library.S3Service") as MockS3_ok:
                        MockEmbed_ok.return_value.delete_item_vectors.return_value = True
                        MockS3_ok.return_value.delete_file.return_value = True
                        try:
                            runner(db)
                        except TypeError:
                            runner()

                    rec_after_v = find_cleanup_record("repro_i2_item", "repro_i2_u", tok_i2_a, "vectors")
                    rec_after_s = find_cleanup_record(
                        "repro_i2s_item", "repro_i2s_u", tok_i2_s, "s3", artifact_key=s3_key_i2)
                    live_after_i2 = refresh_item("repro_i2_live_item")
                    observe("vector artifact record after invoking the real runner", rec_after_v)
                    observe("S3 artifact record after invoking the real runner", rec_after_s)
                    observe("a separate LIVE attempt's own content, must be untouched", live_after_i2.content)

                    check(
                        "a real, autonomous production entry point discovers and resolves "
                        "durable failed cleanup work for exactly the affected item/user/attempt/"
                        "artifact, without touching a separate live attempt's own artifacts",
                        (rec_after_v is None or rec_after_v.get("state") not in ("pending", "failed"))
                        and (rec_after_s is None or rec_after_s.get("state") not in ("pending", "failed"))
                        and live_after_i2.content == live_content_i2,
                        detail=(
                            f"rec_after_v={rec_after_v!r} rec_after_s={rec_after_s!r} "
                            f"live_content={live_after_i2.content!r}"
                        ),
                    )

            # ═════════════════════════════════════════════════════════════
            def _section_j():
                section("J — mixed-version old writer completing after an earlier reconciliation sweep")

                mkuser("repro_j_u", successful_sources_total=0)
                # Represents an old-code worker's item mid-flight when the
                # sweep runs — not yet processed, so it does not match the
                # sweep's filter yet.
                mkitem("repro_j_item", "repro_j_u", processed=False, entitlement_status=None)

                fixed_before, failed_before = ent.reconcile_unaccounted_processed_items(db)
                db.commit()
                observe("real sweep result BEFORE the old worker finishes (fixed, failed)",
                        (fixed_before, failed_before))
                item_after_sweep = refresh_item("repro_j_item")
                observe("item state immediately after the sweep, before the old worker finishes",
                        (item_after_sweep.processed, item_after_sweep.entitlement_status))
                if item_after_sweep.processed or item_after_sweep.entitlement_status is not None:
                    raise RuntimeError(
                        "setup assumption violated — item already looked processed/accounted "
                        "before the old worker's legacy completion")

                # The old worker finishes AFTER the sweep already returned —
                # its own real, narrowest legacy write path: `item.processed
                # = True; db.commit()`, exactly as this repo's own code
                # comments describe pre-remediation code doing (no
                # entitlement_status, no reservation/attempt fields — it has
                # never heard of any of that).
                db.query(LibraryItem).filter(LibraryItem.id == "repro_j_item").update(
                    {"processed": True, "content": "legacy worker's real extracted content"})
                db.commit()

                # Deliberately: no reboot, no manual reconciliation call
                # here — this IS the assertion window the task specifies.
                after_j = refresh_item("repro_j_item")
                user_j = refresh_user("repro_j_u")
                observe("item state after the old worker's legacy completion, sweep NOT re-run",
                        (after_j.processed, after_j.entitlement_status, after_j.content))
                observe("user's successful_sources_total (must reflect this item if truly "
                        "accounted)", user_j.successful_sources_total)

                classification = _classify_mixed_version_write(
                    after_j.processed, after_j.entitlement_status,
                    accounted_hint=(user_j.successful_sources_total >= 1))
                observe("classification (via the same pure classifier the self-check exercises)",
                        classification)

                check(
                    "an old writer's legacy completion after an earlier sweep cannot remain "
                    "successfully processed and accessible while unaccounted — it must be "
                    "immediately accounted for, database-fenced, or durably scheduled for "
                    "reconciliation before the next boot",
                    classification != "processed_accessible_unaccounted",
                    detail=(
                        f"processed={after_j.processed!r} entitlement_status={after_j.entitlement_status!r} "
                        f"content={after_j.content!r} successful_sources_total="
                        f"{user_j.successful_sources_total!r} classification={classification!r} — "
                        f"reconcile_unaccounted_processed_items() only runs at process startup "
                        f"(app/database.py's create_tables() -> "
                        f"_run_task2_required_migrations_and_backfill()); there is no scheduled "
                        f"job, request-time hook, or durable retry queue that would catch an old "
                        f"worker's legacy write landing AFTER that one-time boot sweep already ran"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_k():
                section("K — Premium attempt ownership (no Free-capacity lease to reap)")

                class _FakeIndexK:
                    def __init__(self):
                        self.upsert_calls = []

                    def upsert(self, vectors, namespace):
                        if len(self.upsert_calls) == 0:
                            reached_upsert_k.set()
                            release_upsert_k.wait(timeout=10)
                        self.upsert_calls.append(len(vectors))

                    def delete(self, filter, namespace=None):
                        pass

                fake_index_k = _FakeIndexK()

                def _fake_embedding_service_init_k(self):
                    self.pinecone_available = True
                    self.index = fake_index_k

                n_chunks_k = 250
                fake_chunks_k = [f"chunk {i}" for i in range(n_chunks_k)]
                fake_embeddings_k = [[0.1, 0.2, 0.3, 0.4] for _ in range(n_chunks_k)]

                reached_upsert_k = threading.Event()
                release_upsert_k = threading.Event()
                worker_exc_k = []

                mkuser("repro_k_u", is_premium=True, successful_sources_total=0)
                mkitem("repro_k_item", "repro_k_u", processed=False)

                def _run_worker_k():
                    try:
                        library_router.process_pdf_embeddings(
                            "repro_k_item", b"%PDF-fake-bytes-K", "repro_k_u")
                    except Exception as e:
                        worker_exc_k.append(e)

                with mock.patch("app.routers.library.S3Service") as MockS3_k, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_k), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=fake_chunks_k), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=fake_embeddings_k), \
                     mock.patch("app.services.text_extract.pdf_to_structured_text",
                                 return_value="attempt K-A's real extracted PDF text, long enough to index"):
                    MockS3_k.return_value.upload_file.return_value = "repro_k_u/repro_k_item/attemptA.pdf"
                    MockS3_k.return_value.delete_file.return_value = True

                    t_k = threading.Thread(target=_run_worker_k)
                    t_k.start()
                    tok_k_a = None
                    tok_k_b = None
                    b_content_k = None
                    try:
                        got_reached_k = reached_upsert_k.wait(timeout=10)
                        observe("attempt A's (Premium) worker genuinely reached the first real "
                                "upsert call (in flight)", got_reached_k)
                        if not got_reached_k:
                            raise RuntimeError("worker never reached the first upsert call")

                        item_mid_k = refresh_item("repro_k_item")
                        tok_k_a = item_mid_k.last_processing_attempt_id
                        observe("attempt A's durable identity (Premium — no reservation_lease_token "
                                "at all)", (tok_k_a, item_mid_k.reservation_lease_token))
                        if not tok_k_a or item_mid_k.reservation_lease_token is not None:
                            raise RuntimeError("setup assumption violated — expected a Premium "
                                                "attempt with a durable identity but no capacity lease")

                        # No REAL production code path exists to mint a
                        # genuinely new Premium attempt token for an item
                        # already mid-flight: `reserve_free_capacity`'s own
                        # `_ACTIVE_RESERVATION_STATES` short-circuit makes a
                        # second call for a 'premium' item a pure no-op
                        # (idempotent, no new token), and no other function
                        # in this codebase ever reassigns `last_processing_
                        # attempt_id` for an item that stays 'premium'
                        # throughout — Premium has no reap/TTL mechanism at
                        # all (2B-R1 correction #5: verified by direct
                        # reading of `reserve_free_capacity`, `release_
                        # reservation`, `finalize_successful_processing`,
                        # and `_reap_stale_reservations` — none of them
                        # touch a 'premium' row's status or attempt id).
                        #
                        # Per the task's own narrow-modeling option: reassign
                        # ONLY `last_processing_attempt_id` directly — the
                        # exact atomic ownership-boundary field this whole
                        # section is about — leaving `entitlement_status`
                        # ('premium') and every other field UNCHANGED and
                        # UNINVENTED. This models the single atomic instant
                        # a real replacement/ownership-handoff operation
                        # would perform, without claiming any existing
                        # function performs the surrounding transition.
                        tok_k_b = str(__import__("uuid").uuid4())
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_k_item").update(
                            {"last_processing_attempt_id": tok_k_b})
                        db.commit()
                        item_after_b = refresh_item("repro_k_item")
                        if item_after_b.entitlement_status != "premium":
                            raise RuntimeError(
                                "setup assumption violated — the narrow reassignment must leave "
                                "entitlement_status untouched at 'premium'")
                        observe("attempt B's durable identity (must differ from A) — a narrowly "
                                "modeled reassignment of ONLY last_processing_attempt_id, "
                                "entitlement_status left at 'premium' throughout", tok_k_b)

                        b_content_k = "attempt B's own real content, must survive attempt A's later processing"
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_k_item").update(
                            {"content": b_content_k})
                        db.commit()
                    finally:
                        release_upsert_k.set()
                    t_k.join(timeout=10)

                if worker_exc_k:
                    raise RuntimeError(f"attempt K's worker thread raised {worker_exc_k[0]!r}")
                if t_k.is_alive():
                    raise RuntimeError("attempt K's worker thread did not finish within its join timeout")
                if not tok_k_a or not tok_k_b:
                    raise RuntimeError("setup incomplete — tok_k_a/tok_k_b were not established")

                after_k = refresh_item("repro_k_item")
                observe("upsert call sizes, in order (batch 1 was already in flight when "
                        "ownership was lost)", fake_index_k.upsert_calls)
                observe("item content / last_processing_attempt_id / entitlement_status after "
                        "attempt A's later (stale) processing finished", (
                            after_k.content, after_k.last_processing_attempt_id, after_k.entitlement_status))

                upserts_after_loss_k = max(0, len(fake_index_k.upsert_calls) - 1)
                check(
                    "(Premium) no additional provider/external upsert call begins after attempt A "
                    "has been superseded, even though a Premium attempt has no Free-capacity lease "
                    "for the heartbeat to renew",
                    upserts_after_loss_k == 0,
                    detail=(
                        f"upsert_calls={fake_index_k.upsert_calls!r} — `_AttemptGuard.renew_now()` "
                        f"returns True unconditionally whenever `lease_token is None` (the Premium "
                        f"case), so it can never detect that `last_processing_attempt_id` — the "
                        f"ONLY durable identity a Premium attempt actually has — has been reassigned "
                        f"to a newer attempt"
                    ),
                )
                check(
                    "(Premium) attempt B's own real content survives attempt A's later (stale) "
                    "processing",
                    after_k.content == b_content_k,
                    detail=f"content={after_k.content!r} expected={b_content_k!r}",
                )
                check(
                    "(Premium) attempt A's later, stale completion never finalizes as though it "
                    "still owned the item (last_processing_attempt_id must remain B's)",
                    after_k.last_processing_attempt_id == tok_k_b,
                    detail=(
                        f"last_processing_attempt_id={after_k.last_processing_attempt_id!r} "
                        f"expected={tok_k_b!r} — `finalize_successful_processing` only validates a "
                        f"caller's token when `lease_token is not None`; a Premium attempt's "
                        f"`lease_token` is ALWAYS None, so its finalize call skips token "
                        f"verification entirely and can convert the item under a superseded attempt"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_l():
                section("L — Authoritative pre-side-effect check (cached guard.check() vs "
                        "synchronous database validation)")

                reached_download_l = threading.Event()
                release_download_l = threading.Event()
                began_ocr_call_l = threading.Event()
                worker_exc_l = []

                class _ObservingS3ServiceL:
                    def __init__(self):
                        pass

                    def download_file(self, ref):
                        reached_download_l.set()
                        release_download_l.wait(timeout=10)
                        return b"fake-pdf-bytes-for-ocr-L"

                def _ocr_side_effect_l(pdf_bytes, on_progress=None):
                    began_ocr_call_l.set()
                    return "OCR'd text for section L"

                def _fake_embedding_service_init_l(self):
                    self.pinecone_available = True

                    class _NoOpIndexL:
                        def upsert(self, vectors, namespace):
                            pass

                        def delete(self, filter, namespace=None):
                            pass
                    self.index = _NoOpIndexL()

                mkuser("repro_l_u", successful_sources_total=0)
                mkitem("repro_l_item", "repro_l_u", processed=False, file_url="s3://fake/repro_l_item.pdf")

                def _run_worker_l():
                    try:
                        library_router._run_ocr("repro_l_item", "repro_l_u")
                    except Exception as e:
                        worker_exc_l.append(e)

                with mock.patch("app.routers.library.S3Service", _ObservingS3ServiceL), \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_l), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]]), \
                     mock.patch("app.services.ocr_service.ocr_pdf", side_effect=_ocr_side_effect_l), \
                     _deterministic_heartbeat_gate() as gate_l:
                    t_l = threading.Thread(target=_run_worker_l)
                    t_l.start()
                    tok_l_a = None
                    tok_l_b = None
                    try:
                        got_reached_l = reached_download_l.wait(timeout=10)
                        observe("worker genuinely reached the S3 download (attempt A pauses "
                                "immediately before an external call)", got_reached_l)
                        if not got_reached_l:
                            raise RuntimeError("worker never reached S3Service.download_file")

                        item_mid_l = refresh_item("repro_l_item")
                        tok_l_a = item_mid_l.reservation_lease_token
                        initial_expiry_l = item_mid_l.reservation_lease_expires_at
                        observe("attempt A token, captured while A is still the current owner", tok_l_a)
                        if not tok_l_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Barrier 1: wait for A's OWN background heartbeat to
                        # renew for real at least once — proves this window
                        # begins strictly AFTER "A's latest background
                        # heartbeat completes", not before it ever ran.
                        heartbeat_ticked_l, _ = _poll_for_heartbeat_renewal(
                            "repro_l_item", "repro_l_u", tok_l_a, initial_expiry_l,
                            deadline_seconds=_HEARTBEAT_POLL_DEADLINE_SECONDS)
                        observe("A's own background heartbeat ticked at least once before the "
                                "database ownership change", heartbeat_ticked_l)
                        if not heartbeat_ticked_l:
                            raise RuntimeError("A's background heartbeat never renewed — cannot "
                                                "establish the required barrier order")

                        # Deterministic gate (correction #10): freezes
                        # every FURTHER background-thread renewal from this
                        # exact point — the one real tick just observed
                        # above already landed and is unaffected — so the
                        # window below can never race a second tick,
                        # regardless of real elapsed time.
                        gate_l.pause()

                        # Barrier 2: database ownership changes to B,
                        # synchronously, via the SAME real reap+reserve
                        # primitives every other stale-supersession section
                        # uses — now provably race-free against A's own
                        # heartbeat thread.
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_l_item").update(
                            {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                        db.commit()
                        real_reap("repro_l_u", "repro_l_probe1")
                        if refresh_item("repro_l_item").entitlement_status != "released":
                            raise RuntimeError("expected the real reaper to release repro_l_item")
                        it_l_retry = refresh_item("repro_l_item")
                        ent.reserve_free_capacity(db, it_l_retry, "repro_l_u")
                        tok_l_b = refresh_item("repro_l_item").reservation_lease_token
                        observe("attempt B token (must differ from A)", tok_l_b)
                        if not tok_l_b or tok_l_b == tok_l_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")
                    finally:
                        # Barrier 3: A resumes — its own `guard.check()`
                        # right after the download returns is evaluated
                        # against the guard's LAST observation, which is
                        # still "not lost" because the gate above has
                        # deterministically prevented any further
                        # background renewal from completing.
                        release_download_l.set()
                    t_l.join(timeout=10)
                    # Let the gated background thread (if still paused
                    # mid-renewal) proceed and finish naturally — it now
                    # correctly observes the real superseded state and sets
                    # `_lost`, harmlessly, after the worker has already
                    # joined; the context manager's own `finally` also
                    # releases the gate unconditionally.

                if worker_exc_l:
                    raise RuntimeError(f"section L worker thread raised {worker_exc_l[0]!r}")
                if t_l.is_alive():
                    raise RuntimeError("section L worker thread did not finish within its join timeout")
                if not tok_l_a or not tok_l_b:
                    raise RuntimeError("setup incomplete — tok_l_a/tok_l_b were not established")

                observe("did attempt A begin the OCR provider call after the database ownership "
                        "change (but before A's guard's own next background tick)?",
                        began_ocr_call_l.is_set())
                check(
                    "a checkpoint placed immediately before a paid/external call performs "
                    "AUTHORITATIVE validation (a fresh read of durable ownership state) rather "
                    "than trusting a background thread's LAST cached observation — attempt A must "
                    "not begin the OCR call once database ownership has already changed, even "
                    "when that change lands inside the gap between two heartbeat ticks",
                    not began_ocr_call_l.is_set(),
                    detail=(
                        f"began_ocr_call={began_ocr_call_l.is_set()!r} — `_AttemptGuard.check()` "
                        f"only inspects the in-process `self._lost` flag, set exclusively by the "
                        f"background thread's OWN periodic tick (or a synchronous checkpoint's own "
                        f"renewal); a database ownership change landing strictly between two ticks "
                        f"is invisible to `check()` until the NEXT tick actually runs"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_m():
                section("M — Stale finalization after B has already finalized")

                mkuser("repro_m_u", successful_sources_total=0)
                mkitem("repro_m_item", "repro_m_u", processed=False,
                       content="attempt B's real content for the M end-to-end worker run")
                it_m = refresh_item("repro_m_item")
                ent.reserve_free_capacity(db, it_m, "repro_m_u")
                tok_m_a = refresh_item("repro_m_item").reservation_lease_token
                observe("attempt A token, preserved before supersession", tok_m_a)
                if not tok_m_a:
                    raise RuntimeError("reservation fixture did not produce a token")

                db.query(LibraryItem).filter(LibraryItem.id == "repro_m_item").update(
                    {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                db.commit()
                real_reap("repro_m_u", "repro_m_probe1")
                if refresh_item("repro_m_item").entitlement_status != "released":
                    raise RuntimeError("expected the real reaper to release repro_m_item")
                it_m_retry = refresh_item("repro_m_item")
                ent.reserve_free_capacity(db, it_m_retry, "repro_m_u")
                tok_m_b = refresh_item("repro_m_item").reservation_lease_token
                observe("attempt B token (must differ from A)", tok_m_b)
                if not tok_m_b or tok_m_b == tok_m_a:
                    raise RuntimeError("attempt B did not mint a genuinely new token")

                # Drive B through the REAL end-to-end extraction worker
                # (process_item_embeddings — text pipeline, no S3/OCR needed)
                # so B's finalized state is the product of the real pipeline,
                # not a hand-constructed row.
                class _FakeIndexM:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        pass

                def _fake_embedding_service_init_m(self):
                    self.pinecone_available = True
                    self.index = _FakeIndexM()

                worker_exc_m = []

                def _run_worker_m_b():
                    try:
                        library_router.process_item_embeddings("repro_m_item", "repro_m_u")
                    except Exception as e:
                        worker_exc_m.append(e)

                with mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_m), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]] * 2):
                    t_m_b = threading.Thread(target=_run_worker_m_b)
                    t_m_b.start()
                    t_m_b.join(timeout=10)

                if worker_exc_m:
                    raise RuntimeError(f"attempt B's end-to-end worker raised {worker_exc_m[0]!r}")

                b_finalized = refresh_item("repro_m_item")
                observe("attempt B's real state after its own end-to-end worker finished",
                        (b_finalized.processed, b_finalized.chunk_count, b_finalized.entitlement_status,
                         b_finalized.content))
                if not (b_finalized.processed and b_finalized.entitlement_status == "consumed"
                        and b_finalized.last_processing_attempt_id == tok_m_b):
                    raise RuntimeError("attempt B's real end-to-end worker did not finalize as expected")
                b_snapshot = {
                    "content": b_finalized.content,
                    "chunk_count": b_finalized.chunk_count,
                    "entitlement_status": b_finalized.entitlement_status,
                }

                # Direct finalization-path coverage (isolates the primitive
                # exactly as every real worker calls it): attempt A, whose
                # token was captured and preserved BEFORE supersession, now
                # calls the SAME finalize_successful_processing(...) real
                # workers call, with its own stale token, AFTER B has
                # already, genuinely, successfully finalized.
                item_for_a = refresh_item("repro_m_item")
                accepted_a = ent.finalize_successful_processing(
                    db, item_for_a, "repro_m_u", 999, lease_token=tok_m_a)
                observe("finalize_successful_processing(...)'s return value for attempt A's stale, "
                        "post-B-finalize call", accepted_a)

                after_m = refresh_item("repro_m_item")
                observe("item state after attempt A's stale finalize call", (
                    after_m.content, after_m.chunk_count, after_m.entitlement_status,
                    after_m.last_processing_attempt_id))

                check(
                    "finalize_successful_processing() REJECTS (returns False) a stale attempt's "
                    "token once a different, newer attempt has already finalized the item — never "
                    "reports acceptance to a caller whose token no longer matches",
                    accepted_a is False,
                    detail=(
                        f"accepted_a={accepted_a!r} — the function's very first ownership check is "
                        f"`if locked_item.processed: return True`, which fires BEFORE the "
                        f"`lease_token`/`reservation_lease_token` comparison ever runs; a stale "
                        f"attempt A calling this after B has already finalized is told 'yes, "
                        f"accepted' purely because SOME attempt (not necessarily A) already "
                        f"finished — which then causes A's own caller to skip compensating cleanup "
                        f"for whatever A itself actually wrote"
                    ),
                )
                check(
                    "attempt B's already-committed content/chunk_count/entitlement_status are "
                    "never overwritten by attempt A's later, stale finalize call",
                    after_m.content == b_snapshot["content"]
                    and after_m.chunk_count == b_snapshot["chunk_count"]
                    and after_m.entitlement_status == b_snapshot["entitlement_status"],
                    detail=f"after={after_m.content, after_m.chunk_count, after_m.entitlement_status!r} b_snapshot={b_snapshot!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_n():
                section("N — Duplicate workers must not share ownership (Free + Premium)")

                # Deterministic by construction, not by timing luck: worker A
                # is driven to a point strictly AFTER its own reservation has
                # been durably COMMITTED, then held there on a real barrier.
                # Worker B is only ever started once that commit is
                # confirmed, so B's own reservation attempt is GUARANTEED —
                # never merely likely — to observe an already-active
                # reservation. This removes any dependence on OS thread
                # scheduling or SQLite's coarser (non-row-level) locking
                # behavior, which a naive "start both threads at once" design
                # was found to make genuinely non-deterministic under SQLite
                # specifically (occasionally letting both callers' inserts
                # interleave before either committed).
                def _run_ordered_pair(item_id, user_id, label):
                    reached_a = threading.Event()
                    release_a = threading.Event()
                    provider_calls = {"n": 0}
                    worker_exc = []

                    def _gated_get_embeddings(texts):
                        provider_calls["n"] += 1
                        if provider_calls["n"] == 1:
                            reached_a.set()
                            release_a.wait(timeout=10)
                        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

                    class _FakeIndexN:
                        def upsert(self, vectors, namespace):
                            pass

                        def delete(self, filter, namespace=None):
                            pass

                    def _fake_embedding_service_init_n(self):
                        self.pinecone_available = True
                        self.index = _FakeIndexN()

                    def _run_worker_a():
                        try:
                            library_router.process_item_embeddings(item_id, user_id)
                        except Exception as e:
                            worker_exc.append(e)

                    with mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_n), \
                         mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one", "chunk two"]), \
                         mock.patch.object(es_mod, "_get_embeddings", side_effect=_gated_get_embeddings):
                        t_a = threading.Thread(target=_run_worker_a)
                        t_a.start()
                        got_reached_a = reached_a.wait(timeout=10)
                        if not got_reached_a:
                            release_a.set()
                            t_a.join(timeout=10)
                            raise RuntimeError(f"section N ({label}) worker A never reached the "
                                                f"provider call")

                        item_after_a_reserve = refresh_item(item_id)
                        if item_after_a_reserve.entitlement_status not in ("pending", "premium"):
                            release_a.set()
                            t_a.join(timeout=10)
                            raise RuntimeError(f"section N ({label}) worker A's reservation did "
                                                f"not durably commit before worker B started")

                        # Worker B runs to full completion SYNCHRONOUSLY,
                        # single-threaded, on the main thread — a real,
                        # complete second invocation of the exact same
                        # entry point, guaranteed to observe A's
                        # already-committed reservation.
                        library_router.process_item_embeddings(item_id, user_id)

                        release_a.set()
                        t_a.join(timeout=10)

                    if worker_exc:
                        raise RuntimeError(f"section N ({label}) a duplicate-worker thread raised {worker_exc[0]!r}")
                    if t_a.is_alive():
                        raise RuntimeError(f"section N ({label}) worker A's thread did not finish")
                    return provider_calls["n"]

                # --- Free tier -------------------------------------------
                mkuser("repro_n_free_u", successful_sources_total=0)
                mkitem("repro_n_free_item", "repro_n_free_u", processed=False,
                       content="shared content both duplicate workers will try to index")
                calls_free = _run_ordered_pair("repro_n_free_item", "repro_n_free_u", "Free")
                after_n_free = refresh_item("repro_n_free_item")
                user_n_free = refresh_user("repro_n_free_u")
                observe("Free: real embedding-provider calls made by A (blocked mid-call) and "
                        "B (run to completion while A was durably reserved but still blocked)",
                        calls_free)
                observe("Free: final item state / user's successful_sources_total",
                        (after_n_free.processed, after_n_free.entitlement_status, user_n_free.successful_sources_total))
                check(
                    "(Free) at most ONE worker is admitted, and at most ONE real provider call "
                    "begins, for two duplicate workers deterministically ordered so the second "
                    "starts only after the first's reservation has durably committed",
                    calls_free == 1,
                    detail=(
                        f"provider_calls={calls_free} — `reserve_free_capacity`'s idempotent "
                        f"`if locked_item.entitlement_status in _ACTIVE_RESERVATION_STATES: return "
                        f"True` lets a SECOND caller proceed with the SAME shared token rather than "
                        f"being rejected before it ever reaches the paid embedding call"
                    ),
                )
                check(
                    "(Free) one source still consumes at most one permanent entitlement even when "
                    "two duplicate workers shared one reservation token — `finalize_successful_"
                    "processing`'s own `if locked_item.processed: return True` early-return "
                    "protects the COUNTER even though (see Section M) it misreports success to "
                    "whichever caller finalizes second",
                    user_n_free.successful_sources_total == 1,
                    detail=f"successful_sources_total={user_n_free.successful_sources_total!r}",
                )

                # --- Premium tier ------------------------------------------
                mkuser("repro_n_prem_u", is_premium=True, successful_sources_total=0)
                mkitem("repro_n_prem_item", "repro_n_prem_u", processed=False,
                       content="shared content both duplicate premium workers will try to index")
                calls_prem = _run_ordered_pair("repro_n_prem_item", "repro_n_prem_u", "Premium")
                after_n_prem = refresh_item("repro_n_prem_item")
                observe("Premium: real embedding-provider calls made by the deterministically "
                        "ordered A/B pair", calls_prem)
                observe("Premium: final item state", (after_n_prem.processed, after_n_prem.entitlement_status))
                check(
                    "(Premium) at most ONE worker is admitted, and at most ONE real provider call "
                    "begins, for two deterministically-ordered duplicate workers",
                    calls_prem == 1,
                    detail=f"provider_calls={calls_prem}",
                )
                check(
                    "(Premium) the item ends up processed exactly once, in a single well-defined "
                    "state, regardless of how many duplicate workers raced it",
                    after_n_prem.processed is True and after_n_prem.entitlement_status == "premium",
                    detail=(
                        f"processed={after_n_prem.processed!r} "
                        f"entitlement_status={after_n_prem.entitlement_status!r}"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_o():
                section("O — Post-upload row-mutation fence (PDF archive)")

                store_o = {"objects": {}, "upload_calls": []}
                reached_upload_o = threading.Event()
                release_upload_o = threading.Event()
                reached_extract_o = threading.Event()
                release_extract_o = threading.Event()
                upload_call_count_o = {"n": 0}
                worker_exc_o = []

                class _GatedS3ServiceO:
                    def __init__(self):
                        pass

                    def upload_file(self, file_content, filename, content_type):
                        upload_call_count_o["n"] += 1
                        is_first = upload_call_count_o["n"] == 1
                        if is_first:
                            reached_upload_o.set()
                            release_upload_o.wait(timeout=10)
                        store_o["objects"][filename] = file_content
                        store_o["upload_calls"].append(filename)
                        return filename

                    def delete_file(self, key):
                        if key in store_o["objects"]:
                            del store_o["objects"][key]
                            return True
                        return False

                def _fake_embedding_service_init_o(self):
                    self.pinecone_available = True

                    class _NoOpIndexO:
                        def upsert(self, vectors, namespace):
                            pass

                        def delete(self, filter, namespace=None):
                            pass
                    self.index = _NoOpIndexO()

                def _blocking_pdf_extract_o(pdf_bytes, max_chars):
                    # Only attempt A's OWN call blocks here — B's real
                    # end-to-end run (driven synchronously, on the main
                    # thread, while A is still parked in its upload) must
                    # complete immediately and NOT be caught by this same
                    # shared mock, or B's own worker would stall for up to
                    # the full wait timeout before this section could even
                    # observe B's real committed state.
                    if pdf_bytes == b"%PDF-fake-bytes-O-A":
                        reached_extract_o.set()
                        release_extract_o.wait(timeout=10)
                        return "attempt A's real extracted PDF text, long enough to index for section O"
                    return "attempt B's real extracted PDF text, long enough to index for section O"

                mkuser("repro_o_u", successful_sources_total=0)
                mkitem("repro_o_item", "repro_o_u", processed=False)

                def _run_worker_a_o():
                    try:
                        library_router.process_pdf_embeddings(
                            "repro_o_item", b"%PDF-fake-bytes-O-A", "repro_o_u")
                    except Exception as e:
                        worker_exc_o.append(e)

                with mock.patch("app.routers.library.S3Service", _GatedS3ServiceO), \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_o), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]]), \
                     mock.patch("app.services.text_extract.pdf_to_structured_text",
                                 side_effect=_blocking_pdf_extract_o), \
                     _deterministic_stale_supersession() as _unused_supersede_o:
                    t_a_o = threading.Thread(target=_run_worker_a_o)
                    t_a_o.start()
                    tok_a_o = None
                    tok_b_o = None
                    key_a_o = None
                    key_b_o = None
                    try:
                        got_reached_o = reached_upload_o.wait(timeout=10)
                        observe("attempt A genuinely started its attempt-scoped S3 upload", got_reached_o)
                        if not got_reached_o:
                            raise RuntimeError("attempt A never reached S3Service.upload_file")

                        item_mid_o = refresh_item("repro_o_item")
                        tok_a_o = item_mid_o.last_processing_attempt_id
                        observe("attempt A's durable identity, while A is still the current owner",
                                tok_a_o)

                        # Deterministic supersession (correction #10): B
                        # supersedes A for real (background-thread renewal
                        # made inert by the active patch above, so this
                        # cannot race it), then goes through the SAME real
                        # archive path to completion, establishing its OWN
                        # real, committed archive state.
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_o_item").update(
                            {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                        db.commit()
                        real_reap("repro_o_u", "repro_o_probe1")
                        if refresh_item("repro_o_item").entitlement_status != "released":
                            raise RuntimeError("expected the real reaper to release repro_o_item")

                        library_router.process_pdf_embeddings(
                            "repro_o_item", b"%PDF-fake-bytes-O-B", "repro_o_u")
                        item_after_b_o = refresh_item("repro_o_item")
                        tok_b_o = item_after_b_o.last_processing_attempt_id
                        key_b_o = item_after_b_o.file_url
                        observe("attempt B's durable identity and its own real, committed archive key",
                                (tok_b_o, key_b_o))
                        if not tok_b_o or tok_b_o == tok_a_o or not key_b_o:
                            raise RuntimeError("attempt B's real archive path did not produce a "
                                                "consistent, observable, genuinely new identity/key")
                        if not (item_after_b_o.processed and item_after_b_o.entitlement_status == "consumed"):
                            raise RuntimeError("attempt B's real end-to-end worker did not finalize as expected")
                    finally:
                        # Attempt A's upload now "returns" its own key —
                        # the exact moment the task specifies.
                        release_upload_o.set()

                    # Discover A's real key from the actual upload-call log
                    # (production's own real behavior), never an assumed/
                    # constructed key format — `upload_file` appends to
                    # `upload_calls` immediately after returning from its
                    # own block, before the worker does anything else, so
                    # this is populated regardless of what A does next.
                    if store_o["upload_calls"]:
                        key_a_o = store_o["upload_calls"][0]
                    observe("attempt A's real archive key, discovered from the actual upload-call "
                            "log (never constructed)", key_a_o)

                    # EITHER safe outcome is accepted: A may abort
                    # immediately after upload (never reaching extraction —
                    # `got_reached_extract_o` stays False), or it may
                    # continue only after authoritatively validating
                    # ownership first. Capture an IMMUTABLE SCALAR snapshot
                    # at the observation point — never a mutable ORM object
                    # inspected later, which a subsequent `db.expire_all()`
                    # elsewhere in this section could silently stale-refresh
                    # to a LATER point in time.
                    got_reached_extract_o = reached_extract_o.wait(timeout=10)
                    observe("did attempt A continue past its own upload-commit into extraction?",
                            got_reached_extract_o)
                    row_file_url_snapshot = None
                    row_archive_status_snapshot = None
                    if got_reached_extract_o:
                        row_at_observation = refresh_item("repro_o_item")
                        row_file_url_snapshot = str(row_at_observation.file_url) if row_at_observation.file_url else None
                        row_archive_status_snapshot = (
                            str(row_at_observation.archive_status) if row_at_observation.archive_status else None
                        )
                        observe("row state exactly after attempt A's post-upload commit, before A "
                                "does anything else (immutable scalar snapshot)",
                                (row_file_url_snapshot, row_archive_status_snapshot))
                    release_extract_o.set()
                    t_a_o.join(timeout=10)

                if worker_exc_o:
                    raise RuntimeError(f"section O a worker thread raised {worker_exc_o[0]!r}")
                if t_a_o.is_alive():
                    raise RuntimeError("section O attempt A's worker thread did not finish")
                if not tok_a_o or not tok_b_o or not key_b_o or not key_a_o:
                    raise RuntimeError("section O setup incomplete")

                final_o = refresh_item("repro_o_item")
                final_file_url = str(final_o.file_url) if final_o.file_url else None
                observe("final file_url/archive_status/content after everything settled",
                        (final_file_url, final_o.archive_status, final_o.content))
                observe("live objects remaining in the fake S3 store", list(store_o["objects"].keys()))

                if got_reached_extract_o:
                    row_mutation_safe = (
                        row_file_url_snapshot == key_b_o and row_archive_status_snapshot == "stored"
                    )
                else:
                    # A aborted before ever reaching extraction — never
                    # observed a post-upload row mutation from A at all,
                    # which is itself the OTHER accepted safe outcome.
                    row_mutation_safe = True
                check(
                    "attempt A's post-upload handling is safe — EITHER it never reaches the row-"
                    "mutation/extraction phase at all once superseded (aborts immediately after "
                    "upload), OR it continues only after authoritative validation and never "
                    "overwrites attempt B's already-committed, still-current archive identity",
                    row_mutation_safe,
                    detail=(
                        f"reached_extraction={got_reached_extract_o!r} "
                        f"observed_file_url={row_file_url_snapshot!r} expected_b_key={key_b_o!r} "
                        f"observed_archive_status={row_archive_status_snapshot!r} — "
                        f"process_pdf_embeddings calls `guard.check()` BEFORE the upload but not "
                        f"immediately AROUND the `item.file_url = ...; db.commit()` write itself "
                        f"and never aborts early after the upload returns, so a superseded "
                        f"attempt's upload result is written to the row unconditionally and "
                        f"execution always continues into extraction"
                    ),
                )
                check(
                    "attempt B's own real, live archive object in S3 survives attempt A's later, "
                    "stale processing",
                    key_b_o in store_o["objects"],
                    detail=f"remaining_objects={list(store_o['objects'].keys())} expected_key={key_b_o!r}",
                )
                rec_o_a = find_cleanup_record("repro_o_item", "repro_o_u", tok_a_o, "s3", artifact_key=key_a_o)
                observe("cleanup record found for attempt A's own (never-cleared) archive object",
                        rec_o_a)
                check(
                    "attempt A's own uploaded object is durably cleanup-accounted (deleted, or a "
                    "retryable durable record exists) — never silently orphaned",
                    (key_a_o not in store_o["objects"])
                    or (rec_o_a is not None and rec_o_a.get("state") in ("pending", "failed", "resolved")),
                    detail=f"key_a_o={key_a_o!r} still_present={key_a_o in store_o['objects']!r} record={rec_o_a!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_p():
                section("P — OCR progress and finalize-rejection write order")

                # --- P.progress: a progress checkpoint's WRITE must not
                # land after ownership is already lost -----------------------
                reached_first_progress_p = threading.Event()
                release_first_progress_p = threading.Event()
                worker_exc_p1 = []

                def _fake_embedding_service_init_p1(self):
                    self.pinecone_available = True

                    class _NoOpIndexP1:
                        def upsert(self, vectors, namespace):
                            pass

                        def delete(self, filter, namespace=None):
                            pass
                    self.index = _NoOpIndexP1()

                def _ocr_side_effect_p1(pdf_bytes, on_progress=None):
                    if on_progress:
                        on_progress(1, 100)  # early, legitimate progress — A still owns it
                    reached_first_progress_p.set()
                    release_first_progress_p.wait(timeout=10)
                    if on_progress:
                        on_progress(50, 100)  # the checkpoint under test
                    return "OCR text for section P.progress (should never be committed)"

                mkuser("repro_p1_u", successful_sources_total=0)
                mkitem("repro_p1_item", "repro_p1_u", processed=False, file_url="s3://fake/repro_p1_item.pdf")

                def _run_worker_p1():
                    try:
                        library_router._run_ocr("repro_p1_item", "repro_p1_u")
                    except Exception as e:
                        worker_exc_p1.append(e)

                with mock.patch("app.routers.library.S3Service") as MockS3_p1, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_p1), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]]), \
                     mock.patch("app.services.ocr_service.ocr_pdf", side_effect=_ocr_side_effect_p1), \
                     _deterministic_stale_supersession() as supersede_p1:
                    MockS3_p1.return_value.download_file.return_value = b"fake-pdf-bytes-p1"
                    t_p1 = threading.Thread(target=_run_worker_p1)
                    t_p1.start()
                    tok_p1_a = None
                    tok_p1_b = None
                    try:
                        got_reached_p1 = reached_first_progress_p.wait(timeout=10)
                        observe("worker (P.progress) completed one legitimate progress checkpoint "
                                "and is paused before its NEXT one", got_reached_p1)
                        if not got_reached_p1:
                            raise RuntimeError("worker never reached the first progress checkpoint")

                        item_mid_p1 = refresh_item("repro_p1_item")
                        tok_p1_a = item_mid_p1.reservation_lease_token
                        if not tok_p1_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Deterministic supersession (correction #10).
                        tok_p1_b = supersede_p1("repro_p1_item", "repro_p1_u", "repro_p1_probe1")
                        if tok_p1_b == tok_p1_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")

                        # B's own real, distinguishable progress marker.
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_p1_item").update(
                            {"ocr_pages_done": 777, "ocr_pages_total": 1000, "ocr_status": "running"})
                        db.commit()
                    finally:
                        release_first_progress_p.set()
                    t_p1.join(timeout=10)

                if worker_exc_p1:
                    raise RuntimeError(f"section P.progress worker raised {worker_exc_p1[0]!r}")
                if t_p1.is_alive():
                    raise RuntimeError("section P.progress worker did not finish")
                if not tok_p1_a or not tok_p1_b:
                    raise RuntimeError("section P.progress setup incomplete")

                after_p1 = refresh_item("repro_p1_item")
                observe("final ocr_pages_done/ocr_pages_total (P.progress) — must remain B's 777/1000",
                        (after_p1.ocr_pages_done, after_p1.ocr_pages_total))
                check(
                    "an OCR progress checkpoint validates ownership BEFORE committing its "
                    "ocr_pages_done/ocr_pages_total counters, not only afterward",
                    after_p1.ocr_pages_done == 777 and after_p1.ocr_pages_total == 1000,
                    detail=(
                        f"ocr_pages_done={after_p1.ocr_pages_done!r} ocr_pages_total={after_p1.ocr_pages_total!r} "
                        f"— `_run_ocr`'s `progress()` closure sets `item.ocr_pages_done`/"
                        f"`ocr_pages_total` and calls `db.commit()` UNCONDITIONALLY, THEN checks "
                        f"`guard.renew_now()` — the write happens regardless of current ownership"
                    ),
                )

                # --- P.finalize-reject: a rejected finalize must not
                # unconditionally overwrite ocr_status on a row it no longer
                # owns — correction #2: drives a REAL `_run_ocr` worker for
                # attempt A all the way into ITS OWN real `finalize_
                # successful_processing` call and lets A's OWN production
                # code execute whatever it actually does on rejection —
                # never a copy-pasted mutation performed by the test
                # itself. A deterministic barrier is placed immediately
                # around the real finalize call (not timing-based): `app.
                # routers.library.finalize_successful_processing` is
                # wrapped so A's own call to it blocks until this section
                # has genuinely superseded A with B, then the REAL
                # underlying primitive (captured before patching) runs
                # with A's real, now-stale arguments — so whatever
                # `_run_ocr` does next is its own real caller-side branch,
                # observed, not reproduced. -----------------------------
                worker_exc_p2 = []
                reached_finalize_p2 = threading.Event()
                release_finalize_p2 = threading.Event()
                real_finalize_p2 = ent.finalize_successful_processing

                def _gated_finalize_p2(db_arg, item, user_id, chunk_count, lease_token=None):
                    reached_finalize_p2.set()
                    release_finalize_p2.wait(timeout=15)
                    return real_finalize_p2(db_arg, item, user_id, chunk_count, lease_token=lease_token)

                def _fake_embedding_service_init_p2(self):
                    self.pinecone_available = True

                    class _NoOpIndexP2:
                        def upsert(self, vectors, namespace):
                            pass

                        def delete(self, filter, namespace=None):
                            pass
                    self.index = _NoOpIndexP2()

                mkuser("repro_p2_u", successful_sources_total=0)
                mkitem("repro_p2_item", "repro_p2_u", processed=False, file_url="s3://fake/repro_p2_item.pdf")

                def _run_worker_p2():
                    try:
                        library_router._run_ocr("repro_p2_item", "repro_p2_u")
                    except Exception as e:
                        worker_exc_p2.append(e)

                with mock.patch("app.routers.library.S3Service") as MockS3_p2, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_p2), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=[]), \
                     mock.patch("app.services.ocr_service.ocr_pdf", return_value="OCR text for P2"), \
                     mock.patch("app.routers.library.finalize_successful_processing", side_effect=_gated_finalize_p2), \
                     _deterministic_stale_supersession() as supersede_p2:
                    MockS3_p2.return_value.download_file.return_value = b"fake-pdf-bytes-p2"
                    tok_p2_a = None
                    tok_p2_b = None
                    t_p2 = threading.Thread(target=_run_worker_p2)
                    t_p2.start()
                    try:
                        got_reached_p2 = reached_finalize_p2.wait(timeout=10)
                        observe("attempt A's real worker genuinely reached its own real "
                                "finalize_successful_processing() call, paused immediately before "
                                "it runs", got_reached_p2)
                        if not got_reached_p2:
                            raise RuntimeError("worker never reached finalize_successful_processing")

                        item_mid_p2 = refresh_item("repro_p2_item")
                        tok_p2_a = item_mid_p2.reservation_lease_token
                        if not tok_p2_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Deterministic supersession (correction #10),
                        # while A's real finalize call is genuinely paused
                        # immediately before it runs.
                        tok_p2_b = supersede_p2("repro_p2_item", "repro_p2_u", "repro_p2_probe1")
                        if tok_p2_b == tok_p2_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")
                        # B's own real, distinguishable status marker.
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_p2_item").update(
                            {"ocr_status": "running"})
                        db.commit()
                    finally:
                        # A resumes: its own real finalize call now
                        # actually runs, with A's real (now-stale)
                        # arguments — genuinely rejected by production's
                        # own token check — and `_run_ocr`'s own real
                        # rejection branch executes next, whatever it is.
                        release_finalize_p2.set()
                    t_p2.join(timeout=10)

                if worker_exc_p2:
                    raise RuntimeError(f"section P2 worker raised {worker_exc_p2[0]!r}")
                if t_p2.is_alive():
                    raise RuntimeError("section P2 worker did not finish")
                if not tok_p2_a or not tok_p2_b:
                    raise RuntimeError("section P2 setup incomplete")

                after_p2 = refresh_item("repro_p2_item")
                observe("final ocr_status (P.finalize-reject) — must remain B's 'running'",
                        after_p2.ocr_status)
                check(
                    "a rejected finalize's caller-side cleanup never unconditionally overwrites "
                    "ocr_status on a row a newer attempt now owns",
                    after_p2.ocr_status == "running",
                    detail=(
                        f"ocr_status={after_p2.ocr_status!r} — `_run_ocr`'s `if not accepted: "
                        f"item.ocr_status = \"failed\"; db.commit()` runs with no ownership check "
                        f"of its own, trusting only finalize's boolean result rather than "
                        f"re-verifying `item.last_processing_attempt_id == attempt_token` before "
                        f"writing to the row"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_q():
                section("Q — URL empty-content mutation window")

                class _FakeUrlResponseQ:
                    def __init__(self, text):
                        self.text = text

                reached_q = threading.Event()
                release_q = threading.Event()
                worker_exc_q = []

                def _blocking_fetch_q(url, headers=None, timeout=None):
                    reached_q.set()
                    release_q.wait(timeout=10)
                    # Deliberately EMPTY of extractable text (no article/
                    # main/body content at all) — the real code's
                    # `if not text.strip():` branch below.
                    return _FakeUrlResponseQ("<html><head></head><body></body></html>")

                mkuser("repro_q_u", successful_sources_total=0)
                mkitem("repro_q_item", "repro_q_u", processed=False,
                       title="https://example.invalid/empty-article")

                def _run_worker_q():
                    try:
                        library_router.process_url_embeddings(
                            "repro_q_item", "https://example.invalid/empty-article", "repro_q_u")
                    except Exception as e:
                        worker_exc_q.append(e)

                with mock.patch("app.routers.library.fetch_public_url", side_effect=_blocking_fetch_q), \
                     _deterministic_stale_supersession() as supersede_q:
                    t_q = threading.Thread(target=_run_worker_q)
                    t_q.start()
                    tok_q_a = None
                    tok_q_b = None
                    b_error_q = "attempt B's own real processing_error, must survive"
                    try:
                        got_reached_q = reached_q.wait(timeout=10)
                        observe("worker genuinely entered the real fetch_public_url call", got_reached_q)
                        if not got_reached_q:
                            raise RuntimeError("worker never reached fetch_public_url")

                        item_mid_q = refresh_item("repro_q_item")
                        tok_q_a = item_mid_q.reservation_lease_token
                        if not tok_q_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Deterministic supersession (correction #10).
                        tok_q_b = supersede_q("repro_q_item", "repro_q_u", "repro_q_probe1")
                        if tok_q_b == tok_q_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")

                        db.query(LibraryItem).filter(LibraryItem.id == "repro_q_item").update(
                            {"processing_error": b_error_q, "processed": True})
                        db.commit()
                    finally:
                        release_q.set()
                    t_q.join(timeout=10)

                if worker_exc_q:
                    raise RuntimeError(f"section Q worker raised {worker_exc_q[0]!r}")
                if t_q.is_alive():
                    raise RuntimeError("section Q worker did not finish")
                if not tok_q_a or not tok_q_b:
                    raise RuntimeError("section Q setup incomplete")

                after_q = refresh_item("repro_q_item")
                observe("final processing_error/processed (Q) — must remain B's marker/True",
                        (after_q.processing_error, after_q.processed))
                check(
                    "the URL pipeline's empty-content-error write validates ownership before "
                    "committing processing_error/processed on a row it may no longer own",
                    after_q.processing_error == b_error_q and after_q.processed is True,
                    detail=(
                        f"processing_error={after_q.processing_error!r} processed={after_q.processed!r} "
                        f"— `process_url_embeddings`'s `if not text.strip(): item.processed = False; "
                        f"item.processing_error = ...; db.commit()` runs with NO ownership check "
                        f"before it, unlike the pipeline's successful-completion path, which at "
                        f"least reaches a real `finalize_successful_processing` token check"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_r():
                section("R — Background-only heartbeat database errors")

                # Correction #3: the ORIGINAL version of this section
                # patched `renew_reservation_lease` to raise for EVERY
                # caller — including the WORKER's own synchronous
                # `on_batch=guard.checkpoint` call — so the observed "no
                # second batch begins" outcome was actually caused by an
                # unrelated uncaught exception crashing the checkpoint
                # itself, not by the background heartbeat's error-handling
                # behavior under test. This version raises ONLY for calls
                # genuinely originating from the guard's OWN background
                # thread (identified by its real, production-assigned
                # thread name); the worker's own synchronous checkpoint
                # calls reach the REAL primitive unmodified, so whatever
                # this test observes is driven by production's actual,
                # correctly-functioning real-time checkpoint mechanism —
                # not an artifact of over-broad mocking.
                n_chunks_r = 250  # > 2 upsert batches of 100

                class _FakeIndexR:
                    def __init__(self):
                        self.upsert_calls = []

                    def upsert(self, vectors, namespace):
                        if len(self.upsert_calls) == 0:
                            reached_upsert_r.set()
                            release_upsert_r.wait(timeout=10)
                        self.upsert_calls.append(len(vectors))

                    def delete(self, filter, namespace=None):
                        pass

                fake_index_r = _FakeIndexR()

                def _fake_embedding_service_init_r(self):
                    self.pinecone_available = True
                    self.index = fake_index_r

                reached_upsert_r = threading.Event()
                release_upsert_r = threading.Event()
                worker_exc_r = []
                heartbeat_error_calls_r = {"n": 0}
                real_renew_r = ent.renew_reservation_lease

                def _background_only_raising_renew_r(db_arg, item_id, user_id, lease_token):
                    if threading.current_thread().name.startswith("attempt-guard-"):
                        heartbeat_error_calls_r["n"] += 1
                        raise RuntimeError("simulated heartbeat database outage")
                    return real_renew_r(db_arg, item_id, user_id, lease_token)

                mkuser("repro_r_u", successful_sources_total=0)
                mkitem("repro_r_item", "repro_r_u", processed=False,
                       content="some real content long enough to embed for section R")

                def _run_worker_r():
                    try:
                        library_router.process_pdf_embeddings(
                            "repro_r_item", b"%PDF-fake-bytes-R", "repro_r_u")
                    except Exception as e:
                        worker_exc_r.append(e)

                fake_chunks_r = [f"chunk {i}" for i in range(n_chunks_r)]
                fake_embeddings_r = [[0.1, 0.2, 0.3, 0.4] for _ in range(n_chunks_r)]

                with mock.patch("app.routers.library.S3Service") as MockS3_r, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_r), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=fake_chunks_r), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=fake_embeddings_r), \
                     mock.patch("app.services.text_extract.pdf_to_structured_text",
                                 return_value="attempt R's real extracted PDF text, long enough to index"), \
                     mock.patch("app.routers.library.renew_reservation_lease",
                                 side_effect=_background_only_raising_renew_r):
                    MockS3_r.return_value.upload_file.return_value = "repro_r_u/repro_r_item/attemptR.pdf"
                    MockS3_r.return_value.delete_file.return_value = True
                    t_r = threading.Thread(target=_run_worker_r)
                    t_r.start()
                    tok_r_a = None
                    tok_r_b = None
                    try:
                        got_reached_r = reached_upsert_r.wait(timeout=10)
                        observe("attempt A's worker genuinely reached the first real upsert call "
                                "(in flight)", got_reached_r)
                        if not got_reached_r:
                            raise RuntimeError("worker never reached the first upsert call")

                        item_mid_r = refresh_item("repro_r_item")
                        tok_r_a = item_mid_r.reservation_lease_token
                        if not tok_r_a:
                            raise RuntimeError("reservation fixture did not produce a token")

                        # Give the guard's own background thread a real
                        # chance to attempt (and fail) at least once before
                        # supersession — proves the errors genuinely
                        # happened, not merely that the window was too short
                        # to observe them.
                        deadline_r = time.monotonic() + (library_router._HEARTBEAT_INTERVAL_SECONDS + 1.5)
                        while heartbeat_error_calls_r["n"] < 1 and time.monotonic() < deadline_r:
                            time.sleep(0.05)
                        observe("real background-heartbeat database calls that raised, before "
                                "supersession", heartbeat_error_calls_r["n"])
                        if heartbeat_error_calls_r["n"] < 1:
                            raise RuntimeError("the background heartbeat never even attempted a "
                                                "renewal within the wait window — cannot evaluate "
                                                "its error-handling behavior")

                        # Genuine supersession. No deterministic-barrier
                        # helper is needed here specifically: the
                        # background thread's renewal is scoped to ALWAYS
                        # raise (never succeed) for the ENTIRE duration of
                        # this patch, so it can never renew the lease
                        # regardless of real elapsed time — this sequence
                        # is inherently race-free against it by construction.
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_r_item").update(
                            {"reservation_lease_expires_at": NOW - datetime.timedelta(seconds=1)})
                        db.commit()
                        real_reap("repro_r_u", "repro_r_probe1")
                        if refresh_item("repro_r_item").entitlement_status != "released":
                            raise RuntimeError("expected the real reaper to release repro_r_item")
                        it_r_retry = refresh_item("repro_r_item")
                        ent.reserve_free_capacity(db, it_r_retry, "repro_r_u")
                        tok_r_b = refresh_item("repro_r_item").reservation_lease_token
                        if not tok_r_b or tok_r_b == tok_r_a:
                            raise RuntimeError("attempt B did not mint a genuinely new token")
                        b_content_r = "attempt B's own real content, must survive attempt A's later processing"
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_r_item").update(
                            {"content": b_content_r})
                        db.commit()
                    finally:
                        release_upsert_r.set()
                    t_r.join(timeout=15)

                if worker_exc_r:
                    raise RuntimeError(f"section R worker raised {worker_exc_r[0]!r}")
                if t_r.is_alive():
                    raise RuntimeError("section R worker did not finish within its join timeout")
                if not tok_r_a or not tok_r_b:
                    raise RuntimeError("section R setup incomplete")

                after_r = refresh_item("repro_r_item")
                observe("upsert call sizes, in order", fake_index_r.upsert_calls)
                observe("final entitlement_status/content (R) after everything settled",
                        (after_r.entitlement_status, after_r.content))
                upserts_after_loss_r = max(0, len(fake_index_r.upsert_calls) - 1)
                check(
                    "attempt A cannot start a second real provider batch, or mutate/finalize, "
                    "after ownership loss — even while the BACKGROUND heartbeat thread itself is "
                    "failing with real database errors — because the worker's OWN synchronous "
                    "on_batch checkpoint (a real, authoritative database check, independent of "
                    "the background thread) still correctly detects the loss and raises "
                    "AttemptOwnershipLost before any further batch begins",
                    upserts_after_loss_r == 0,
                    detail=(
                        f"upsert_calls={fake_index_r.upsert_calls!r} heartbeat_error_calls="
                        f"{heartbeat_error_calls_r['n']!r} — the worker's own `on_batch=guard."
                        f"checkpoint` calls the REAL `renew_reservation_lease` (unaffected by "
                        f"this section's background-thread-only mock), which correctly returns "
                        f"False once B owns the token, so `checkpoint()` raises for real"
                    ),
                )
                check(
                    "attempt B's own real content survives attempt A's later processing even "
                    "while A's BACKGROUND heartbeat mechanism was failing with real database "
                    "errors the whole time",
                    after_r.content == b_content_r,
                    detail=f"content={after_r.content!r} expected={b_content_r!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_s():
                section("S — Guard termination: the worker must not report clean completion "
                        "while its background heartbeat thread is still alive")

                class _FakeIndexS:
                    def upsert(self, vectors, namespace):
                        pass

                    def delete(self, filter, namespace=None):
                        pass

                def _fake_embedding_service_init_s(self):
                    self.pinecone_available = True
                    self.index = _FakeIndexS()

                reached_embed_s = threading.Event()
                release_embed_s = threading.Event()
                reached_renewal_s = threading.Event()
                release_renewal_s = threading.Event()
                worker_exc_s = []

                real_renew = ent.renew_reservation_lease

                def _selective_renew_s(db_arg, item_id, user_id, lease_token):
                    if threading.current_thread().name.startswith("attempt-guard-"):
                        reached_renewal_s.set()
                        release_renewal_s.wait(timeout=20)
                        return True
                    return real_renew(db_arg, item_id, user_id, lease_token)

                def _blocking_get_embeddings_s(texts):
                    reached_embed_s.set()
                    release_embed_s.wait(timeout=10)
                    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

                mkuser("repro_s_u", successful_sources_total=0)
                mkitem("repro_s_item", "repro_s_u", processed=False,
                       content="some real content long enough to embed for section S")

                def _run_worker_s():
                    try:
                        library_router.process_item_embeddings("repro_s_item", "repro_s_u")
                    except Exception as e:
                        worker_exc_s.append(e)

                worker_finished_at = {"t": None}
                with mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_s), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one"]), \
                     mock.patch.object(es_mod, "_get_embeddings", side_effect=_blocking_get_embeddings_s), \
                     mock.patch("app.routers.library.renew_reservation_lease", side_effect=_selective_renew_s):
                    t_s = threading.Thread(target=_run_worker_s)
                    start_t = time.monotonic()
                    t_s.start()

                    got_reached_embed_s = reached_embed_s.wait(timeout=10)
                    observe("worker genuinely entered the pre-batch embedding phase", got_reached_embed_s)
                    if not got_reached_embed_s:
                        release_renewal_s.set()
                        raise RuntimeError("worker never reached _get_embeddings")

                    # Hold the worker blocked for longer than one heartbeat
                    # interval so the guard's OWN background thread genuinely
                    # attempts (and, per the mock above, gets stuck inside) a
                    # real renewal before the worker itself resumes.
                    got_reached_renewal_s = reached_renewal_s.wait(
                        timeout=library_router._HEARTBEAT_INTERVAL_SECONDS + 5)
                    observe("the guard's OWN background thread genuinely entered a real renewal "
                            "call and is now blocked inside it", got_reached_renewal_s)
                    if not got_reached_renewal_s:
                        release_embed_s.set()
                        release_renewal_s.set()
                        raise RuntimeError("the background heartbeat thread never entered a "
                                            "renewal call within the wait window")

                    # Now let the WORKER itself finish normally — its own
                    # synchronous checkpoint call is routed to the REAL
                    # renew (see the mock above), so finalize can genuinely
                    # succeed. The background thread remains stuck.
                    release_embed_s.set()
                    t_s.join(timeout=library_router._HEARTBEAT_INTERVAL_SECONDS + 10)
                    worker_finished_at["t"] = time.monotonic() - start_t

                    # Track ONLY this section's exact guard thread name
                    # (`_AttemptGuard.start()` names it deterministically
                    # after the item id) — never a process-global prefix
                    # scan, per correction #6.
                    _guard_thread_name_s = "attempt-guard-repro_s_item"
                    background_alive_at_worker_completion = any(
                        th.name == _guard_thread_name_s and th.is_alive()
                        for th in threading.enumerate()
                    )
                    observe("worker thread finished (joined) after ~%.1fs" % worker_finished_at["t"],
                            not t_s.is_alive())
                    observe("is the guard's background heartbeat thread STILL alive at the moment "
                            "the worker reports completion?", background_alive_at_worker_completion)

                    # Clean up OUR OWN test thread before this section ends —
                    # never leave a real thread running past this section.
                    release_renewal_s.set()
                    deadline_cleanup = time.monotonic() + 10
                    lingering = [
                        th for th in threading.enumerate()
                        if th.name == _guard_thread_name_s and th.is_alive()
                    ]
                    for th in lingering:
                        th.join(timeout=max(0.0, deadline_cleanup - time.monotonic()))
                    still_lingering = [
                        th for th in threading.enumerate()
                        if th.name == _guard_thread_name_s and th.is_alive()
                    ]
                    observe("attempt-guard thread(s) still alive after this section's own cleanup "
                            "(must be empty)", [th.name for th in still_lingering])
                    if still_lingering:
                        harness_errors.append(
                            f"section S: {len(still_lingering)} attempt-guard thread(s) survived "
                            f"this section's own cleanup — a real thread leak, not a product invariant"
                        )

                # Correction #6: an UNRELATED exception (anything the worker
                # itself did not deliberately surface as a lifecycle-
                # shutdown signal) remains a genuine harness/setup problem
                # — current production has no distinct "lifecycle shutdown
                # failure" exception type at all (verified by reading
                # `process_item_embeddings`'s exception handling, which
                # only ever catches `AttemptOwnershipLost`/`EmbeddingError`/
                # generic `Exception` and never re-raises), so any exception
                # reaching here is by construction NOT that second accepted
                # outcome under today's code.
                if worker_exc_s:
                    raise RuntimeError(f"section S worker raised {worker_exc_s[0]!r}")
                if t_s.is_alive():
                    raise RuntimeError("section S worker thread did not finish within its join timeout")

                worker_surfaced_lifecycle_failure_s = len(worker_exc_s) > 0  # always False today — see above
                check(
                    "EITHER (a) Section S's own exact guard thread is stopped and joined before "
                    "the worker reports completion, OR (b) the worker surfaces a specific "
                    "lifecycle-shutdown failure and does not report successful completion",
                    (not background_alive_at_worker_completion) or worker_surfaced_lifecycle_failure_s,
                    detail=(
                        f"background_alive_at_completion={background_alive_at_worker_completion!r} "
                        f"worker_surfaced_lifecycle_failure={worker_surfaced_lifecycle_failure_s!r} "
                        f"— `_AttemptGuard.stop()` calls `self._thread.join(timeout=5)` and returns "
                        f"unconditionally regardless of whether the join actually succeeded, so a "
                        f"background thread genuinely stuck inside a slow/hung database call is "
                        f"simply abandoned — still running, still holding its own open session — "
                        f"while the worker function itself returns as though shutdown were clean, "
                        f"with no lifecycle-shutdown signal of any kind surfaced to distinguish this "
                        f"from a genuinely clean exit"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_t():
                section("T — Ledger-persistence failure fences cleanup (implementation-neutral)")

                import app.database as _database_mod_t

                def _make_fail_first_commit_session():
                    """Implementation-neutral durable-persistence-failure
                    injector (correction #12): wraps the REAL SessionLocal
                    so the FIRST `.commit()` CALL made on ANY session opened
                    while this patch is active raises — modeling a failure
                    at the actual transaction/commit boundary itself,
                    compatible with ORM `add()`+`commit()`, Core `insert()`+
                    execute, or any other durable-ledger implementation
                    that must eventually commit a transaction to persist.
                    Deliberately keyed on the first COMMIT, not the first
                    SESSION INSTANTIATION — a read-only session opened
                    earlier in the same call path (e.g. to look up an
                    item's user_id) never calls `.commit()` at all, so
                    counting instantiations would let the wrong, later
                    session's write sail through untouched. Never assumes a
                    specific ORM class/`__init__` exists."""
                    real_session_local = _database_mod_t.SessionLocal
                    state = {"failed": False}

                    class _FailFirstCommitSession:
                        def __init__(self):
                            self._real = real_session_local()

                        def commit(self):
                            if not state["failed"]:
                                state["failed"] = True
                                raise RuntimeError(
                                    "simulated durable persistence failure at the real "
                                    "transaction/commit boundary")
                            return self._real.commit()

                        def __getattr__(self, name):
                            return getattr(self._real, name)

                    return _FailFirstCommitSession

                # --- T.vectors ---------------------------------------------
                delete_call_log_t = []

                def _fake_delete_item_vectors_t(self, item_id, user_id=None, attempt_token=None):
                    delete_call_log_t.append((item_id, user_id, attempt_token))
                    return True

                mkuser("repro_t_u", successful_sources_total=0)
                it_t = mkitem("repro_t_item", "repro_t_u", processed=False)
                ent.reserve_free_capacity(db, it_t, "repro_t_u")
                tok_t = refresh_item("repro_t_item").reservation_lease_token
                if not tok_t:
                    raise RuntimeError("reservation fixture did not produce a token")

                with mock.patch.object(_database_mod_t, "SessionLocal", _make_fail_first_commit_session()), \
                     mock.patch.object(EmbeddingService, "delete_item_vectors", _fake_delete_item_vectors_t):
                    ok_t = library_router._cleanup_vectors_after_abandoned_processing(
                        "repro_t_item", "repro_t_u", attempt_token=tok_t, reason="section T repro")

                observe("[vectors] _cleanup_vectors_after_abandoned_processing(...)'s return value "
                        "when the durable pending-ledger commit itself failed", ok_t)
                observe("[vectors] real provider delete_item_vectors() calls made despite the "
                        "failed pending-ledger commit", delete_call_log_t)

                rec_t = find_cleanup_record("repro_t_item", "repro_t_u", tok_t, "vectors")
                observe("[vectors] any durable cleanup record found after the fact", rec_t)

                check(
                    "[vectors] when the durable pending-cleanup record's commit itself fails, the "
                    "provider delete is not attempted until that durable state genuinely exists",
                    len(delete_call_log_t) == 0,
                    detail=(
                        f"delete_calls={delete_call_log_t!r} — `_cleanup_vectors_after_abandoned_"
                        f"processing` calls `_cleanup_ledger_upsert_pending(...)` and then proceeds "
                        f"UNCONDITIONALLY to the provider delete regardless of whether that call "
                        f"actually persisted anything; `_cleanup_ledger_upsert_pending` itself "
                        f"catches every exception internally (`except Exception: db.rollback(); "
                        f"logger.exception(...)`) and returns no signal at all"
                    ),
                )
                check(
                    "[vectors] the helper's own return value reflects whether cleanup was "
                    "genuinely, durably handled — not merely whether the provider call itself "
                    "happened to succeed with no durable record behind it",
                    ok_t is False,
                    detail=(
                        f"ok_t={ok_t!r} rec_t={rec_t!r} — since `_cleanup_ledger_upsert_pending` "
                        f"never raises and never returns a status, its caller cannot distinguish "
                        f"'the pending record is durably written' from 'that write silently failed'; "
                        f"the eventual return value here is driven purely by the provider call's own "
                        f"result, with no durable trace anywhere that cleanup was ever attempted"
                    ),
                )

                # --- T.s3 ---------------------------------------------------
                delete_call_log_t_s3 = []

                class _FakeS3T:
                    def __init__(self):
                        pass

                    def delete_file(self, key):
                        delete_call_log_t_s3.append(key)
                        return True

                mkuser("repro_t3_u", successful_sources_total=0)
                it_t3 = mkitem("repro_t3_item", "repro_t3_u", processed=False)
                ent.reserve_free_capacity(db, it_t3, "repro_t3_u")
                tok_t3 = refresh_item("repro_t3_item").reservation_lease_token
                s3_key_t3 = f"repro_t3_u/repro_t3_item/{tok_t3}.pdf"
                if not tok_t3:
                    raise RuntimeError("reservation fixture did not produce a token")

                with mock.patch.object(_database_mod_t, "SessionLocal", _make_fail_first_commit_session()), \
                     mock.patch("app.routers.library.S3Service", _FakeS3T):
                    ok_t3 = library_router._cleanup_archive_after_abandoned_processing(
                        "repro_t3_item", tok_t3, s3_key_t3, reason="section T.s3 repro")

                observe("[s3] _cleanup_archive_after_abandoned_processing(...)'s return value when "
                        "the durable pending-ledger commit itself failed", ok_t3)
                observe("[s3] real provider delete_file() calls made despite the failed "
                        "pending-ledger commit", delete_call_log_t_s3)
                rec_t3 = find_cleanup_record("repro_t3_item", "repro_t3_u", tok_t3, "s3", artifact_key=s3_key_t3)
                observe("[s3] any durable cleanup record found after the fact", rec_t3)

                check(
                    "[s3] when the durable pending-cleanup record's commit itself fails, the "
                    "provider delete is not attempted until that durable state genuinely exists",
                    len(delete_call_log_t_s3) == 0,
                    detail=f"delete_calls={delete_call_log_t_s3!r}",
                )
                check(
                    "[s3] the helper's own return value reflects whether cleanup was genuinely, "
                    "durably handled",
                    ok_t3 is False,
                    detail=f"ok_t3={ok_t3!r} rec_t3={rec_t3!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _call_archive_compensation_signature_aware(fn, item_id, user_id, attempt_token,
                                                             artifact_key, reason):
                """Signature-aware invocation (correction #13): inspects
                the REAL callable's parameters and supplies each semantic
                identity field only under whichever accepted name it
                actually declares — item_id/id, attempt_token/token,
                s3_key/artifact_key/key, user_id (only if the signature
                declares it — current production does not), reason/
                message. A safe implementation that ADDS a `user_id`
                parameter (removing the row lookup this section's own
                first check exposes) becomes callable, and GREEN-capable,
                with no edit to this test. Does not attempt to model an
                arbitrary immutable-identity-OBJECT parameter shape — see
                the completion report's remaining-limitations section."""
                sig = inspect.signature(fn)
                params = sig.parameters
                kwargs = {}

                def _pick(names, value):
                    for n in names:
                        if n in params:
                            kwargs[n] = value
                            return True
                    return False

                _pick(("item_id", "id"), item_id)
                _pick(("attempt_token", "token"), attempt_token)
                _pick(("s3_key", "artifact_key", "key"), artifact_key)
                _pick(("user_id",), user_id)
                _pick(("reason", "message"), reason)
                return fn(**kwargs)

            def _section_u():
                section("U — Cleanup survives live-row deletion")

                store_u = {"objects": {}}

                class _FakeS3U:
                    def __init__(self):
                        pass

                    def delete_file(self, key):
                        return store_u["objects"].pop(key, None) is not None

                mkuser("repro_u_u", successful_sources_total=0)
                it_u = mkitem("repro_u_item", "repro_u_u", processed=False)
                ent.reserve_free_capacity(db, it_u, "repro_u_u")
                tok_u = refresh_item("repro_u_item").reservation_lease_token
                s3_key_u = f"repro_u_u/repro_u_item/{tok_u}.pdf"
                store_u["objects"][s3_key_u] = b"the real archive bytes"
                if not tok_u:
                    raise RuntimeError("reservation fixture did not produce a token")

                # Preserve identity, then remove the live row — the exact
                # scenario this section targets: item/user/attempt/S3-key
                # identity is fully known and immutable, but the
                # `library_items` row itself is gone (a real, independent
                # deletion racing this attempt's own compensation).
                db.query(LibraryItem).filter(LibraryItem.id == "repro_u_item").delete()
                db.commit()
                if refresh_item("repro_u_item") is not None:
                    raise RuntimeError("setup failed — the live row was not actually removed")

                with mock.patch("app.routers.library.S3Service", _FakeS3U):
                    ok_archive_u = _call_archive_compensation_signature_aware(
                        library_router._cleanup_archive_after_abandoned_processing,
                        "repro_u_item", "repro_u_u", tok_u, s3_key_u, "section U repro")

                observe("_cleanup_archive_after_abandoned_processing(...)'s return value after the "
                        "live row was already deleted", ok_archive_u)
                observe("live objects remaining in the fake S3 store", list(store_u["objects"].keys()))
                rec_u_s3 = find_cleanup_record("repro_u_item", "repro_u_u", tok_u, "s3", artifact_key=s3_key_u)
                observe("durable cleanup record for the archive object after the live row was "
                        "deleted", rec_u_s3)

                check(
                    "attempt-scoped archive cleanup either deletes the exact object or leaves it "
                    "durably retryable, even when the `library_items` row itself has already been "
                    "deleted — never silently reports success with no provider call and no ledger "
                    "record",
                    (s3_key_u not in store_u["objects"]) or (rec_u_s3 is not None),
                    detail=(
                        f"ok={ok_archive_u!r} object_still_present={s3_key_u in store_u['objects']!r} "
                        f"record={rec_u_s3!r} — `_cleanup_archive_after_abandoned_processing`'s very "
                        f"first step is `item = db.query(LibraryItem)...first(); if not item: return "
                        f"True` — a deleted row makes this function silently report success while "
                        f"never even attempting the S3 delete or writing any durable ledger record, "
                        f"because it needs `item.user_id` before it can do either"
                    ),
                )

                # Control/contrast: the VECTOR-cleanup half of the same
                # compensation does NOT look up the LibraryItem row at all
                # (item_id/user_id are passed in directly) — it should
                # remain correctly functional even after the row is gone.
                delete_calls_u_vec = []

                def _fake_delete_vectors_u(self, item_id, user_id=None, attempt_token=None):
                    delete_calls_u_vec.append((item_id, attempt_token))
                    return True

                with mock.patch.object(EmbeddingService, "delete_item_vectors", _fake_delete_vectors_u):
                    ok_vectors_u = library_router._cleanup_vectors_after_abandoned_processing(
                        "repro_u_item", "repro_u_u", attempt_token=tok_u, reason="section U vectors control")
                observe("vector cleanup after the live row was deleted (control case)",
                        (ok_vectors_u, delete_calls_u_vec))
                check(
                    "(control) vector cleanup — which never needs to look up the `library_items` "
                    "row — remains correctly functional after that row has already been deleted",
                    ok_vectors_u is True and len(delete_calls_u_vec) == 1,
                    detail=f"ok={ok_vectors_u!r} delete_calls={delete_calls_u_vec!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_v():
                section("V — Real concurrent first-insert race for the same cleanup-ledger "
                        "identity (disposable local PostgreSQL, two independent sessions)")

                # Correction #7: a genuine two-session race, not a
                # sequential SQLite-based proof-split. SQLite's coarse
                # whole-database write lock made a real two-thread race
                # against `_cleanup_ledger_upsert_pending`'s own internal
                # SessionLocal() non-deterministic (sometimes BOTH callers'
                # commits were rejected outright with "database is locked"
                # instead of the intended unique-constraint conflict) — a
                # SQLite testing-infrastructure limitation, not something a
                # real production Postgres deployment would ever exhibit.
                # This version drives the REAL production helper against a
                # real, disposable local PostgreSQL cluster, with
                # `app.database.SessionLocal` temporarily repointed at it,
                # and TWO real, independent sessions synchronized by a
                # barrier so both genuinely observe "no existing row"
                # before either commits.
                try:
                    with _disposable_postgres_cluster("nibbler-v-pg-") as (pg_engine, pg_conn_kwargs):
                        Base.metadata.create_all(bind=pg_engine)

                        import app.database as database_mod_v
                        from app.models.library import CleanupTask as _CleanupTaskClsV
                        from sqlalchemy.orm import sessionmaker as _sessionmaker_v
                        import psycopg2
                        import uuid as _uuid_v

                        PgSessionLocal_v = _sessionmaker_v(bind=pg_engine)

                        # Real fixture rows, against REAL Postgres.
                        pg_setup_session = PgSessionLocal_v()
                        try:
                            u_v = User(id="repro_v_u", email="repro_v_u@example.com", created_at=OLD)
                            pg_setup_session.add(u_v)
                            pg_setup_session.commit()
                            it_v = LibraryItem(id="repro_v_item", user_id="repro_v_u", type="pdf",
                                                title="repro_v_item", processed=False)
                            pg_setup_session.add(it_v)
                            pg_setup_session.commit()
                            ent.reserve_free_capacity(pg_setup_session, it_v, "repro_v_u")
                            tok_v = pg_setup_session.query(LibraryItem).filter(
                                LibraryItem.id == "repro_v_item").first().reservation_lease_token
                        finally:
                            pg_setup_session.close()
                        if not tok_v:
                            raise _PostgresCapabilityError("reservation fixture against real "
                                                             "Postgres did not produce a token")

                        barrier_v = threading.Barrier(2, timeout=15)
                        worker_exc_v = []
                        commit_outcomes_v = {}  # caller label -> "ok" | "integrity_error" | other

                        class _BarrierPgSession:
                            """Wraps a REAL session bound to the disposable
                            Postgres engine; synchronizes the exact query
                            `_cleanup_ledger_upsert_pending` uses to decide
                            'does a row already exist for this identity' —
                            forcing BOTH real sessions to genuinely observe
                            'no existing row' before either commits its own
                            insert, which is what makes the resulting
                            unique-constraint conflict a REAL, reproduced
                            race under real Postgres MVCC/row-locking."""
                            def __init__(self):
                                self._real = PgSessionLocal_v()

                            def query(self, *a, **kw):
                                q = self._real.query(*a, **kw)
                                if a and a[0] is _CleanupTaskClsV:
                                    orig_first = q.first

                                    def _wrapped_first(*a2, **kw2):
                                        result = orig_first(*a2, **kw2)
                                        try:
                                            barrier_v.wait()
                                        except threading.BrokenBarrierError:
                                            pass
                                        return result
                                    q.first = _wrapped_first
                                return q

                            def __getattr__(self, name):
                                return getattr(self._real, name)

                        def _run_upsert_v(label, reason):
                            try:
                                library_router._cleanup_ledger_upsert_pending(
                                    "repro_v_item", "repro_v_u", tok_v, "vectors", None, reason)
                                commit_outcomes_v[label] = "no_exception_observed"
                            except Exception as e:
                                worker_exc_v.append((label, e))

                        with mock.patch.object(database_mod_v, "SessionLocal", _BarrierPgSession):
                            t_v1 = threading.Thread(target=_run_upsert_v, args=("caller-1", "caller-1's real reason"))
                            t_v2 = threading.Thread(target=_run_upsert_v, args=("caller-2", "caller-2's real reason"))
                            t_v1.start()
                            t_v2.start()
                            t_v1.join(timeout=20)
                            t_v2.join(timeout=20)

                        if t_v1.is_alive() or t_v2.is_alive():
                            raise _PostgresCapabilityError(
                                "V: a concurrent upsert thread did not finish — could not place "
                                "the required concurrency barrier faithfully")
                        # `_cleanup_ledger_upsert_pending` itself never lets
                        # an exception escape (its own broad `except
                        # Exception: db.rollback()`), so `worker_exc_v`
                        # being non-empty means something OUTSIDE that
                        # contract broke — a real setup problem here, not
                        # the expected shape of a real race.
                        if worker_exc_v:
                            raise _PostgresCapabilityError(f"V: unexpected exception(s) from the "
                                                             f"real race: {worker_exc_v!r}")

                        verify_session_v = PgSessionLocal_v()
                        try:
                            rows_v = verify_session_v.query(_CleanupTaskClsV).filter(
                                _CleanupTaskClsV.item_id == "repro_v_item",
                                _CleanupTaskClsV.attempt_token == tok_v,
                                _CleanupTaskClsV.artifact_kind == "vectors",
                            ).all()
                        finally:
                            verify_session_v.close()
                        observe("CleanupTask row(s) for this exact identity after the REAL "
                                "concurrent two-session Postgres race",
                                [(r.id, r.cleanup_state, r.reason) for r in rows_v])

                        check(
                            "[real Postgres] exactly one durable logical cleanup-ledger record "
                            "exists for one (item, attempt, artifact-kind) identity after two "
                            "genuinely concurrent, barrier-synchronized first-time insert attempts",
                            len(rows_v) == 1 and rows_v[0].cleanup_state == "pending",
                            detail=f"rows={[(r.id, r.cleanup_state) for r in rows_v]!r}",
                        )
                        check(
                            "[real Postgres] no cleanup attempt becomes unaccounted — the single "
                            "surviving record carries a real reason from ONE of the two genuine "
                            "callers, not a corrupted/empty value",
                            len(rows_v) == 1 and rows_v[0].reason in (
                                "caller-1's real reason", "caller-2's real reason"),
                            detail=f"reason={rows_v[0].reason!r}" if rows_v else "no row survived",
                        )
                        check(
                            "[real Postgres] neither caller's real invocation raised an exception "
                            "that escaped `_cleanup_ledger_upsert_pending`'s own contract — a real "
                            "uniqueness conflict is recovered (caught and rolled back) INSIDE the "
                            "function, not propagated as a crash to either caller",
                            not worker_exc_v,
                            detail=f"worker_exc_v={worker_exc_v!r}",
                        )

                        # Deterministic, no concurrency required, against
                        # the SAME real Postgres database: does the
                        # function's own CONTRACT let a caller tell "my
                        # write durably landed" apart from "someone else's
                        # identical-identity row already existed" apart
                        # from "a genuine failure occurred"?
                        with mock.patch.object(database_mod_v, "SessionLocal", PgSessionLocal_v):
                            direct_result_v = library_router._cleanup_ledger_upsert_pending(
                                "repro_v_item", "repro_v_u", tok_v, "vectors", None, "a third, later reason")
                        check(
                            "[real Postgres] the durable pending-ledger upsert reports its own "
                            "persistence outcome to its caller (a return value distinguishing "
                            "success from a lost uniqueness race), rather than a caller only ever "
                            "being able to log-and-continue with no signal at all",
                            direct_result_v is not None,
                            detail=(
                                f"return value={direct_result_v!r} — the function has no `return` "
                                f"statement of its own at all, so EVERY caller — including the "
                                f"loser of the real race proven above — receives the exact same "
                                f"`None`, with no way to tell 'my write durably landed' from 'it "
                                f"silently lost a race'"
                            ),
                        )
                except _PostgresCapabilityError as e:
                    check_capability(
                        "[real Postgres] a genuine two-session concurrency barrier for Section V "
                        "could be placed and exercised against a real, disposable local "
                        "PostgreSQL cluster",
                        False,
                        detail=f"{e}",
                    )

            # ═════════════════════════════════════════════════════════════
            def _section_w():
                section("W — Continuous autonomous cleanup retry (real registered scheduler)")

                import app.services.notification_service as notif_mod
                from app.models.library import CleanupTask as _CleanupTaskClsW

                mkuser("repro_w_u", successful_sources_total=0)
                it_w = mkitem("repro_w_item", "repro_w_u", processed=False)
                ent.reserve_free_capacity(db, it_w, "repro_w_u")
                tok_w = refresh_item("repro_w_item").reservation_lease_token
                if not tok_w:
                    raise RuntimeError("reservation fixture did not produce a token")

                # A real, durably-failed cleanup task, inserted AFTER
                # startup — exactly the scenario a real production restart
                # would leave behind for an autonomous mechanism to pick up.
                db.add(_CleanupTaskClsW(
                    id=str(__import__("uuid").uuid4()), item_id="repro_w_item", user_id="repro_w_u",
                    attempt_token=tok_w, artifact_kind="vectors", cleanup_state="failed",
                    reason="section W setup — a real failed cleanup task inserted after startup",
                ))
                db.commit()

                scheduler_error_w = None
                jobs_w = []
                try:
                    notif_mod.start_scheduler(lambda: SessionLocal())
                    jobs_w = list(notif_mod.scheduler.get_jobs())
                except Exception as e:
                    scheduler_error_w = e
                finally:
                    try:
                        notif_mod.stop_scheduler()
                    except Exception:
                        pass

                if scheduler_error_w is not None:
                    raise RuntimeError(f"could not start the real production scheduler: {scheduler_error_w!r}")

                observe("real registered jobs on the actual production APScheduler instance",
                        [(j.id, j.func) for j in jobs_w])

                def _job_is_cleanup_retry_candidate(job):
                    fn = job.func
                    name = getattr(fn, "__name__", "")
                    try:
                        src = inspect.getsource(fn)
                    except (OSError, TypeError):
                        src = ""
                    if name in _CLEANUP_RETRY_EXCLUDED_NAMES:
                        return False
                    if "cleanup_state" in src or "cleanup_detail" in src:
                        return True
                    if "retry_cleanup_tasks" in src or "CleanupTask" in src:
                        return True
                    return False

                matching_jobs_w = [j for j in jobs_w if _job_is_cleanup_retry_candidate(j)]
                observe("registered job(s) that semantically reference durable cleanup-retry work",
                        [(j.id, j.func) for j in matching_jobs_w])

                # Correction #8: not just static source inspection —
                # ACTUALLY EXECUTE every real registered job's own real
                # scheduler-facing callable (bounded, via a real event
                # loop, exactly the shape APScheduler itself calls it with:
                # `job.func(**job.kwargs)`), then prove via genuine
                # execution — not inference — that the durable failed
                # cleanup task inserted above remains completely untouched.
                # `db_factory` is repointed at THIS test's own SessionLocal
                # (mirroring `main.py`'s real `_db_factory`), so the real
                # job runs against real, disposable state — zero PushToken
                # rows exist in this test database, so its own real body
                # (streak-alert / pre-generation / delivery passes) finds
                # nothing to act on and makes no external call.
                import asyncio as _asyncio_w
                task_row_w = db.query(_CleanupTaskClsW).filter(
                    _CleanupTaskClsW.item_id == "repro_w_item").first()
                before_snapshot_w = (task_row_w.cleanup_state, task_row_w.reason, task_row_w.retry_count,
                                     task_row_w.claimed_by)

                job_exec_errors_w = []
                for job in jobs_w:
                    try:
                        _asyncio_w.run(job.func(**job.kwargs))
                    except Exception as e:
                        job_exec_errors_w.append((job.id, e))
                observe("real execution errors (if any) from actually calling every registered "
                        "job's own callable", job_exec_errors_w)

                db.expire_all()
                task_row_w_after = db.query(_CleanupTaskClsW).filter(
                    _CleanupTaskClsW.item_id == "repro_w_item").first()
                after_snapshot_w = (task_row_w_after.cleanup_state, task_row_w_after.reason,
                                    task_row_w_after.retry_count, task_row_w_after.claimed_by)
                observe("durable failed cleanup task's state before / after REALLY executing "
                        "every registered job's own callable", (before_snapshot_w, after_snapshot_w))

                check(
                    "the autonomous cleanup-retry runner is wired to a REAL, registered production "
                    "scheduler/maintenance trigger — proven by ACTUALLY EXECUTING every job "
                    "currently registered on the real scheduler and observing that the durable "
                    "failed cleanup task inserted after startup gets discovered/claimed/resolved — "
                    "not merely a callable that exists but is never invoked, and not merely a "
                    "static source-level guess",
                    after_snapshot_w != before_snapshot_w,
                    detail=(
                        f"before={before_snapshot_w!r} after={after_snapshot_w!r} "
                        f"registered_jobs={[j.id for j in jobs_w]!r} "
                        f"semantically_matching_jobs={[j.id for j in matching_jobs_w]!r} — "
                        f"`retry_cleanup_tasks` is currently invoked ONLY once, synchronously, at "
                        f"application startup (inside `app/database.py`'s `_run_task2_required_"
                        f"migrations_and_backfill`); the real, live `notification_service."
                        f"scheduler` has exactly one registered job — `daily_bite_reminder` → "
                        f"`_run_delivery_cycle` — which was genuinely executed above and, as its "
                        f"own source confirms, has nothing to do with cleanup retry, so a durable "
                        f"failed cleanup task inserted after the last boot has NO path back to "
                        f"autonomous retry before the next full application restart"
                    ),
                )
                if job_exec_errors_w:
                    harness_errors.append(
                        f"section W: real execution of {len(job_exec_errors_w)} registered job(s) "
                        f"raised unexpectedly: {job_exec_errors_w!r} — an environment problem "
                        f"running the real job, not a product invariant")

                remaining_w = threading.enumerate()
                stray_scheduler_threads_w = [
                    th for th in remaining_w
                    if "apscheduler" in th.name.lower() and th.is_alive() and th is not threading.current_thread()
                ]
                observe("APScheduler-related threads still alive after stop_scheduler()",
                        [th.name for th in stray_scheduler_threads_w])
                if stray_scheduler_threads_w:
                    harness_errors.append(
                        f"section W: {len(stray_scheduler_threads_w)} scheduler thread(s) survived "
                        f"stop_scheduler() — a real thread leak, not a product invariant"
                    )

            # ═════════════════════════════════════════════════════════════
            def _section_x():
                section("X — Claim-token-bound runner completion")

                store_x = {"deleted": []}
                reached_delete_x = threading.Event()
                release_delete_x = threading.Event()
                delete_call_count_x = {"n": 0}
                worker_exc_x = []

                def _gated_delete_item_vectors_x(self, item_id, user_id=None, attempt_token=None):
                    # Earlier sections (V, W) deliberately leave their own
                    # durable pending/failed 'vectors' CleanupTask rows
                    # un-resolved (that IS what they're each testing) — a
                    # real autonomous runner scanning the WHOLE table will
                    # legitimately encounter those too. Only THIS section's
                    # own target identity should block; every other
                    # attempt_token resolves immediately, exactly as a real
                    # healthy retry would.
                    if attempt_token == tok_x:
                        delete_call_count_x["n"] += 1
                        if delete_call_count_x["n"] == 1:
                            reached_delete_x.set()
                            release_delete_x.wait(timeout=15)
                    store_x["deleted"].append(attempt_token)
                    return True

                mkuser("repro_x_u", successful_sources_total=0)
                it_x = mkitem("repro_x_item", "repro_x_u", processed=False)
                ent.reserve_free_capacity(db, it_x, "repro_x_u")
                tok_x = refresh_item("repro_x_item").reservation_lease_token
                if not tok_x:
                    raise RuntimeError("reservation fixture did not produce a token")
                library_router._cleanup_ledger_upsert_pending(
                    "repro_x_item", "repro_x_u", tok_x, "vectors", None, "section X setup")

                def _run_runner_a_x():
                    # A genuinely SEPARATE session — `retry_cleanup_tasks`
                    # takes an externally-provided Session, and sharing the
                    # main thread's own `db` object across threads would be
                    # undefined-behavior territory for SQLAlchemy, not a
                    # faithful two-runner reproduction.
                    db_a = SessionLocal()
                    try:
                        with mock.patch.object(EmbeddingService, "delete_item_vectors", _gated_delete_item_vectors_x):
                            ent.retry_cleanup_tasks(db_a)
                    except Exception as e:
                        worker_exc_x.append(e)
                    finally:
                        db_a.close()

                t_x = threading.Thread(target=_run_runner_a_x)
                t_x.start()
                got_reached_x = reached_delete_x.wait(timeout=10)
                observe("Runner A genuinely claimed the task and is now blocked inside its real "
                        "provider delete call", got_reached_x)
                if not got_reached_x:
                    release_delete_x.set()
                    t_x.join(timeout=10)
                    raise RuntimeError("Runner A never reached the provider delete")

                from app.models.library import CleanupTask as _CleanupTaskClsX
                db.expire_all()
                row_mid_x = db.query(_CleanupTaskClsX).filter(
                    _CleanupTaskClsX.item_id == "repro_x_item", _CleanupTaskClsX.attempt_token == tok_x,
                    _CleanupTaskClsX.artifact_kind == "vectors").first()
                observe("the row's real claim, observed via production's own claimed_by/claimed_until "
                        "columns, while Runner A is still working it",
                        (row_mid_x.claimed_by, row_mid_x.claimed_until))
                if not row_mid_x.claimed_by:
                    release_delete_x.set()
                    t_x.join(timeout=10)
                    raise RuntimeError("Runner A did not record a real claim on the row")

                # Force Runner A's claim to have expired — a real, durable
                # database state any second runner (a genuinely later
                # scheduled tick, or an operator retry) would observe.
                db.query(_CleanupTaskClsX).filter(_CleanupTaskClsX.id == row_mid_x.id).update(
                    {"claimed_until": NOW - datetime.timedelta(minutes=1)})
                db.commit()

                # Runner B reclaims and produces a DISTINGUISHABLE
                # authoritative result — correction #1: B's provider call
                # deliberately FAILS (a real, distinct error), so B's own
                # correct final state (failed, B's own reason, retry_count
                # incremented) can never be confused with what Runner A's
                # own (always-succeeding) provider mock would produce
                # (resolved, reason=None) — unlike the ORIGINAL version of
                # this section, where both runners' successes happened to
                # converge on the identical visible state, making a real
                # clobber indistinguishable from B's own legitimate result.
                def _failing_delete_item_vectors_x_b(self, item_id, user_id=None, attempt_token=None):
                    return False
                with mock.patch.object(EmbeddingService, "delete_item_vectors", _failing_delete_item_vectors_x_b):
                    ent.retry_cleanup_tasks(db)

                db.expire_all()
                row_after_b_x = db.query(_CleanupTaskClsX).filter(_CleanupTaskClsX.id == row_mid_x.id).first()
                b_authoritative_snapshot = (
                    row_after_b_x.cleanup_state, row_after_b_x.retry_count, row_after_b_x.claimed_by,
                )
                observe("row state after Runner B reclaimed and produced its OWN distinct, "
                        "authoritative (failed) result, while A is still parked",
                        (row_after_b_x.cleanup_state, row_after_b_x.reason, row_after_b_x.retry_count,
                         row_after_b_x.claimed_by))
                if row_after_b_x.cleanup_state != "failed" or row_after_b_x.retry_count < 1:
                    release_delete_x.set()
                    t_x.join(timeout=10)
                    raise RuntimeError("Runner B did not genuinely reclaim and record its own "
                                        "distinct failure after A's claim expired")

                # Now release Runner A — its own (late, always-succeeding)
                # provider call completes and it proceeds to resolve the
                # SAME row with its own outcome.
                release_delete_x.set()
                t_x.join(timeout=15)
                if worker_exc_x:
                    raise RuntimeError(f"section X Runner A raised {worker_exc_x[0]!r}")

                db.expire_all()
                row_final_x = db.query(_CleanupTaskClsX).filter(_CleanupTaskClsX.id == row_mid_x.id).first()
                observe("row state after Runner A's late completion resumed and finished",
                        (row_final_x.cleanup_state, row_final_x.reason, row_final_x.retry_count,
                         row_final_x.claimed_by))

                final_snapshot_x = (row_final_x.cleanup_state, row_final_x.retry_count, row_final_x.claimed_by)
                check(
                    "only the CURRENT claim owner may transition a cleanup task's state — a runner "
                    "(A) whose claim has since expired and been reclaimed by another runner (B) "
                    "cannot overwrite B's own distinct, authoritative result (state, reason, retry "
                    "count) with its own late, unrelated outcome",
                    final_snapshot_x == b_authoritative_snapshot,
                    detail=(
                        f"B's authoritative result={b_authoritative_snapshot!r} "
                        f"final result after A's late completion={final_snapshot_x!r} — "
                        f"`_cleanup_ledger_resolve(item_id, attempt_token, artifact_kind, ok, ...)` "
                        f"looks the row up ONLY by (item_id, attempt_token, artifact_kind) — it "
                        f"never checks `claimed_by`/`claimed_until` at all, so Runner A's late "
                        f"resolution unconditionally overwrites whatever Runner B (the current, "
                        f"legitimate claim holder) already wrote, converting B's real recorded "
                        f"failure back into a false 'resolved'"
                    ),
                )
                check(
                    "every runner thread created by this section terminates before the section ends",
                    not t_x.is_alive(),
                    detail=f"t_x.is_alive()={t_x.is_alive()!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_y():
                section("Y — Malformed tasks and S3 retry row consistency")

                from app.models.library import CleanupTask as _CleanupTaskClsY
                import uuid as _uuid_y

                # --- Y1: unknown artifact_kind -----------------------------
                mkuser("repro_y1_u", successful_sources_total=0)
                db.add(_CleanupTaskClsY(
                    id=str(_uuid_y.uuid4()), item_id="repro_y1_item", user_id="repro_y1_u",
                    attempt_token="tok_y1", artifact_kind="bogus_kind", cleanup_state="failed",
                    reason="malformed — unknown artifact kind",
                ))
                db.commit()

                # --- Y2: s3 kind with a missing key ------------------------
                mkuser("repro_y2_u", successful_sources_total=0)
                db.add(_CleanupTaskClsY(
                    id=str(_uuid_y.uuid4()), item_id="repro_y2_item", user_id="repro_y2_u",
                    attempt_token="tok_y2", artifact_kind="s3", artifact_key=None, cleanup_state="failed",
                    reason="malformed — s3 kind with no key",
                ))
                db.commit()

                with mock.patch("app.routers.library.EmbeddingService") as MockEmbed_y, \
                     mock.patch("app.routers.library.S3Service") as MockS3_y:
                    MockEmbed_y.return_value.delete_item_vectors.return_value = True
                    MockS3_y.return_value.delete_file.return_value = True
                    ent.retry_cleanup_tasks(db)

                row_y1 = db.query(_CleanupTaskClsY).filter(
                    _CleanupTaskClsY.item_id == "repro_y1_item").first()
                row_y2 = db.query(_CleanupTaskClsY).filter(
                    _CleanupTaskClsY.item_id == "repro_y2_item").first()
                observe("Y1 (unknown artifact_kind) state after the real runner ran", row_y1.cleanup_state)
                observe("Y2 (s3, missing key) state after the real runner ran", row_y2.cleanup_state)

                check(
                    "a task with an unrecognized artifact_kind is never marked resolved without "
                    "any actual deletion — it stays failed/operator-visible",
                    row_y1.cleanup_state != "resolved",
                    detail=(
                        f"state={row_y1.cleanup_state!r} — `retry_cleanup_tasks`'s branch for an "
                        f"unrecognized `artifact_kind` (or an 's3' task with no key) is `else: ok = "
                        f"True  # nothing meaningful left to retry` — silently marks it resolved "
                        f"with zero provider calls and zero operator visibility"
                    ),
                )
                check(
                    "an S3 cleanup task with no artifact_key is never marked resolved without any "
                    "actual deletion",
                    row_y2.cleanup_state != "resolved",
                    detail=f"state={row_y2.cleanup_state!r}",
                )

                # --- Y3: successful S3 retry row consistency ---------------
                store_y3 = {"objects": set()}
                delete_calls_y3 = []

                class _FakeS3Y3:
                    def __init__(self):
                        pass

                    def delete_file(self, key):
                        delete_calls_y3.append(key)
                        store_y3["objects"].discard(key)
                        return True

                mkuser("repro_y3_u", successful_sources_total=0)
                it_y3 = mkitem("repro_y3_item", "repro_y3_u", processed=False)
                ent.reserve_free_capacity(db, it_y3, "repro_y3_u")
                tok_y3 = refresh_item("repro_y3_item").reservation_lease_token
                key_y3 = f"repro_y3_u/repro_y3_item/{tok_y3}.pdf"
                store_y3["objects"].add(key_y3)
                db.query(LibraryItem).filter(LibraryItem.id == "repro_y3_item").update(
                    {"file_url": key_y3, "archive_status": "stored"})
                db.commit()
                db.add(_CleanupTaskClsY(
                    id=str(_uuid_y.uuid4()), item_id="repro_y3_item", user_id="repro_y3_u",
                    attempt_token=tok_y3, artifact_kind="s3", artifact_key=key_y3, cleanup_state="failed",
                    reason="Y3 setup",
                ))
                db.commit()
                # Deterministically OLDEST `updated_at` (correction #14) —
                # guarantees this is the FIRST row `retry_cleanup_tasks`'s
                # own `.order_by(updated_at.asc())` scan reaches, never
                # relying on insertion-order timing luck against whatever
                # leftover unresolved rows earlier sections may have left.
                DETERMINISTIC_OLD_Y3 = NOW - datetime.timedelta(days=1)
                db.query(_CleanupTaskClsY).filter(
                    _CleanupTaskClsY.item_id == "repro_y3_item").update({"updated_at": DETERMINISTIC_OLD_Y3})
                db.commit()

                # A separate, untouched item/key must survive — given a
                # deterministically NEWER `updated_at` so it can never be
                # reached first even if the batch happens to be small.
                mkuser("repro_y3b_u", successful_sources_total=0)
                it_y3b = mkitem("repro_y3b_item", "repro_y3b_u", processed=False)
                ent.reserve_free_capacity(db, it_y3b, "repro_y3b_u")
                tok_y3b = refresh_item("repro_y3b_item").reservation_lease_token
                key_y3b = f"repro_y3b_u/repro_y3b_item/{tok_y3b}.pdf"
                store_y3["objects"].add(key_y3b)
                db.query(LibraryItem).filter(LibraryItem.id == "repro_y3b_item").update(
                    {"file_url": key_y3b, "archive_status": "stored"})
                db.commit()
                db.add(_CleanupTaskClsY(
                    id=str(_uuid_y.uuid4()), item_id="repro_y3b_item", user_id="repro_y3b_u",
                    attempt_token=tok_y3b, artifact_kind="s3", artifact_key=key_y3b, cleanup_state="pending",
                    reason="Y3 control — must remain untouched",
                ))
                db.commit()
                db.query(_CleanupTaskClsY).filter(
                    _CleanupTaskClsY.item_id == "repro_y3b_item").update(
                        {"updated_at": NOW + datetime.timedelta(days=1)})
                db.commit()
                control_ledger_before = db.query(_CleanupTaskClsY).filter(
                    _CleanupTaskClsY.item_id == "repro_y3b_item").first()
                control_snapshot_before = (
                    control_ledger_before.cleanup_state, control_ledger_before.claimed_by,
                    control_ledger_before.claimed_until, control_ledger_before.artifact_key,
                )

                # Isolated in a clean ledger for this specific retry call —
                # batch_size covers only this section's own 2 rows,
                # deterministically ordered by the explicit updated_at
                # values above, independent of any earlier section's
                # leftover unresolved rows (which, with `updated_at`
                # left at their own natural insert time around "now",
                # sort AFTER the deliberately-old target and BEFORE the
                # deliberately-future control — irrelevant either way
                # since this call only processes 2 rows total).
                with mock.patch("app.routers.library.S3Service", _FakeS3Y3), \
                     mock.patch("app.routers.library.EmbeddingService") as MockEmbed_y3:
                    MockEmbed_y3.return_value.delete_item_vectors.return_value = True
                    # batch_size=1 is the actual isolation mechanism — the
                    # deterministic updated_at values above only decide
                    # WHICH single row this exact call reaches (the
                    # deliberately-oldest target), guaranteeing the
                    # deliberately-newest control is never included in this
                    # specific call at all.
                    ent.retry_cleanup_tasks(db, batch_size=1)

                item_y3_after = refresh_item("repro_y3_item")
                item_y3b_after = refresh_item("repro_y3b_item")
                ledger_y3_after = db.query(_CleanupTaskClsY).filter(
                    _CleanupTaskClsY.item_id == "repro_y3_item").first()
                ledger_y3b_after = db.query(_CleanupTaskClsY).filter(
                    _CleanupTaskClsY.item_id == "repro_y3b_item").first()
                observe("[target] real delete_file() calls, in order", delete_calls_y3)
                observe("Y3 target: object present / row file_url,archive_status / ledger state,claim "
                        "after real retry",
                        (key_y3 in store_y3["objects"], item_y3_after.file_url, item_y3_after.archive_status,
                         ledger_y3_after.cleanup_state, ledger_y3_after.claimed_by, ledger_y3_after.claimed_until))
                observe("Y3 control: a different item's key/row/ledger/claim, must remain fully "
                        "untouched",
                        (key_y3b in store_y3["objects"], item_y3b_after.file_url,
                         ledger_y3b_after.cleanup_state, ledger_y3b_after.claimed_by))

                check(
                    "a successful autonomous S3 retry deletes the exact key, calling the provider "
                    "with exactly that key",
                    key_y3 not in store_y3["objects"] and delete_calls_y3 == [key_y3],
                    detail=f"delete_calls={delete_calls_y3!r} expected_key={key_y3!r}",
                )
                check(
                    "the target ledger record resolves, and its claim fields are cleared",
                    ledger_y3_after.cleanup_state == "resolved"
                    and ledger_y3_after.claimed_by is None and ledger_y3_after.claimed_until is None,
                    detail=(
                        f"state={ledger_y3_after.cleanup_state!r} claimed_by={ledger_y3_after.claimed_by!r} "
                        f"claimed_until={ledger_y3_after.claimed_until!r}"
                    ),
                )
                check(
                    "the matching LibraryItem row's file_url/archive_status are cleared, since they "
                    "still identify the exact key that was just deleted — not just the ledger row",
                    item_y3_after.file_url is None and item_y3_after.archive_status is None,
                    detail=(
                        f"file_url={item_y3_after.file_url!r} archive_status={item_y3_after.archive_status!r} "
                        f"— `retry_cleanup_tasks` delegates resolution to `_cleanup_ledger_resolve`, "
                        f"which clears `LibraryItem.cleanup_state`/`cleanup_detail` on success but "
                        f"never touches `file_url`/`archive_status` at all — only the DIRECT "
                        f"compensation path (`_cleanup_archive_after_abandoned_processing`) does that"
                    ),
                )
                control_snapshot_after = (
                    ledger_y3b_after.cleanup_state, ledger_y3b_after.claimed_by,
                    ledger_y3b_after.claimed_until, ledger_y3b_after.artifact_key,
                )
                check(
                    "a different, newer/unrelated key, its row, and its ledger record (state, "
                    "claim, artifact key) all remain completely untouched by this retry",
                    key_y3b in store_y3["objects"] and item_y3b_after.file_url == key_y3b
                    and control_snapshot_after == control_snapshot_before,
                    detail=f"object_present={key_y3b in store_y3['objects']!r} file_url={item_y3b_after.file_url!r} "
                           f"ledger_before={control_snapshot_before!r} ledger_after={control_snapshot_after!r}",
                )

            # ═════════════════════════════════════════════════════════════
            def _section_z():
                section("Z — Post-finalization image ownership")

                store_z = {"objects": {}, "delete_stored_calls": []}
                reached_extract_z = threading.Event()
                release_extract_z = threading.Event()

                def _blocking_extract_and_store_z(**kwargs):
                    reached_extract_z.set()
                    release_extract_z.wait(timeout=10)
                    key = f"book-images/{kwargs.get('user_id')}/{kwargs.get('item_id')}/imgA.png"
                    store_z["objects"][key] = b"real uploaded image bytes"
                    return [{"id": "img_A1", "key": key, "w": 100, "h": 100}]

                def _fake_delete_stored_z(images):
                    # Correction #9: patches the REAL cleanup boundary
                    # (`app.services.image_extract.delete_stored`) so it
                    # shares the SAME controlled in-memory object store as
                    # the upload side above — no real `S3Service` is ever
                    # instantiated or called by either half of this
                    # section, keeping it fully hermetic.
                    for img in (images or []):
                        key = (img or {}).get("key")
                        if key:
                            store_z["delete_stored_calls"].append(key)
                            store_z["objects"].pop(key, None)

                def _fake_embedding_service_init_z(self):
                    self.pinecone_available = True

                    class _NoOpIndexZ:
                        def upsert(self, vectors, namespace):
                            pass

                        def delete(self, filter, namespace=None):
                            pass
                    self.index = _NoOpIndexZ()

                worker_exc_z = []
                mkuser("repro_z_u", successful_sources_total=0)
                mkitem("repro_z_item", "repro_z_u", processed=False)

                def _run_worker_z():
                    try:
                        library_router.process_pdf_embeddings(
                            "repro_z_item", b"%PDF-fake-bytes-Z", "repro_z_u")
                    except Exception as e:
                        worker_exc_z.append(e)

                with mock.patch("app.routers.library.S3Service") as MockS3_z, \
                     mock.patch.object(EmbeddingService, "__init__", _fake_embedding_service_init_z), \
                     mock.patch.object(es_mod, "_chunk_text", return_value=["chunk one"]), \
                     mock.patch.object(es_mod, "_get_embeddings", return_value=[[0.1, 0.2, 0.3, 0.4]]), \
                     mock.patch("app.services.text_extract.pdf_to_structured_text",
                                 return_value="attempt Z's real extracted PDF text, long enough to index"), \
                     mock.patch("app.services.image_extract.extract_and_store",
                                 side_effect=_blocking_extract_and_store_z), \
                     mock.patch("app.services.image_extract.delete_stored",
                                 side_effect=_fake_delete_stored_z):
                    MockS3_z.return_value.upload_file.return_value = "repro_z_u/repro_z_item/attemptZ.pdf"
                    MockS3_z.return_value.delete_file.return_value = True

                    t_z = threading.Thread(target=_run_worker_z)
                    t_z.start()
                    try:
                        got_reached_z = reached_extract_z.wait(timeout=10)
                        observe("worker reached real image extraction, AFTER text/content finalize "
                                "already committed successfully", got_reached_z)
                        if not got_reached_z:
                            raise RuntimeError("worker never reached extract_and_store")

                        finalized_mid_z = refresh_item("repro_z_item")
                        observe("item is already genuinely processed/finalized at this point",
                                (finalized_mid_z.processed, finalized_mid_z.content is not None))
                        if not finalized_mid_z.processed:
                            raise RuntimeError("attempt A had not actually finalized before image "
                                                "extraction began — setup assumption violated")

                        # The live row is now DELETED out from under the
                        # still-in-flight image extraction — a real,
                        # independent deletion racing this attempt's own
                        # post-finalization work.
                        db.query(LibraryItem).filter(LibraryItem.id == "repro_z_item").delete()
                        db.commit()
                        if refresh_item("repro_z_item") is not None:
                            raise RuntimeError("setup failed to remove the live row")
                    finally:
                        release_extract_z.set()
                    t_z.join(timeout=10)

                if worker_exc_z:
                    raise RuntimeError(f"section Z worker raised {worker_exc_z[0]!r}")
                if t_z.is_alive():
                    raise RuntimeError("section Z worker did not finish")

                observe("real image object(s) uploaded by the (now-orphaned) extraction call, and "
                        "any real delete_stored() calls made against them",
                        (list(store_z["objects"].keys()), store_z["delete_stored_calls"]))

                # Correction #9: EITHER safe outcome is accepted — object
                # genuinely removed (delete_stored resolved it), OR a
                # durable, semantically discoverable cleanup record exists
                # for the exact user/item/attempt/image key (reusing the
                # SAME reflective cleanup-ledger discovery every other
                # section's `find_cleanup_record` uses, so a future
                # implementation that records image cleanup work in that
                # same durable ledger — under any artifact_kind naming —
                # becomes GREEN with no edit to this test).
                objects_cleared_z = len(store_z["objects"]) == 0
                durable_record_z = None
                for kind_guess in ("image", "images", "book_image"):
                    rec = find_cleanup_record("repro_z_item", "repro_z_u", None, kind_guess)
                    if rec is not None:
                        durable_record_z = rec
                        break
                if durable_record_z is None:
                    # Broader net: ANY durable record at all for this item,
                    # regardless of attempt/kind naming, whose key/reason
                    # references the exact orphaned image key.
                    ledger_cls, fields = _find_cleanup_ledger_model()
                    if ledger_cls is not None:
                        candidate_rows = db.query(ledger_cls).filter(
                            getattr(ledger_cls, fields["item"]) == "repro_z_item").all()
                        for row in candidate_rows:
                            row_key = getattr(row, fields["key"]) if fields.get("key") else None
                            if row_key and row_key in store_z["delete_stored_calls"] + list(store_z["objects"].keys()):
                                durable_record_z = row
                                break
                observe("any durable, discoverable cleanup record found for the orphaned image "
                        "artifact, under any plausible artifact-kind naming", durable_record_z)

                check(
                    "an image object uploaded by extraction that raced a real row deletion is "
                    "EITHER genuinely removed, OR durably accounted for somewhere discoverable — "
                    "never silently orphaned with zero durable trace and no successful cleanup",
                    objects_cleared_z or durable_record_z is not None,
                    detail=(
                        f"objects_cleared={objects_cleared_z!r} orphaned_objects="
                        f"{list(store_z['objects'].keys())!r} durable_record={durable_record_z!r} "
                        f"delete_stored_calls={store_z['delete_stored_calls']!r} (empty — see below) "
                        f"— `_extract_book_images` uploads real objects via `extract_and_store` and "
                        f"then does `item.images = images; db.commit()` with NO ownership/existence "
                        f"check of its own; when the row is gone, that commit raises a real "
                        f"`StaleDataError`. Empirically confirmed (via a direct, isolated repro "
                        f"outside this file, since production code cannot be edited to add a trace): "
                        f"`_extract_book_images` itself — despite its own docstring's explicit "
                        f"'never raises' guarantee, and despite having BOTH an inner AND an outer "
                        f"`except Exception` around this exact code path — does NOT actually catch "
                        f"this failure; the real `delete_stored(images)` boundary (hermetically "
                        f"mocked above, sharing this test's own object store) is NEVER reached at "
                        f"all, and the exception instead propagates all the way out to `process_pdf_"
                        f"embeddings`'s own top-level handler (which does catch it there, so the "
                        f"worker itself still completes without crashing this test — but the images "
                        f"'never raises' contract is genuinely broken under this exact race, a "
                        f"deeper defect than the originally-reported orphaned upload alone)"
                    ),
                )

            # ═════════════════════════════════════════════════════════════
            def _section_mixed():
                section("MIXED-1 — old readers: a processed=True/entitlement_status=NULL-or-"
                        "released item must not be servable by a reader that only ever checks "
                        "`processed`")

                mkuser("repro_mx1_u", successful_sources_total=0)
                mkitem("repro_mx1_item", "repro_mx1_u", processed=False, entitlement_status=None,
                       content="pre-existing content, present before the legacy write")

                # The real, continuous database trigger — installed at
                # `create_tables()` time, already active for this whole
                # run — is what's under test here, not a manually-invoked
                # helper: a genuine old-code write, through the ORM's own
                # `Query.update()` (which bypasses Python-level SQLAlchemy
                # event hooks, exactly like a real old-code background task).
                db.query(LibraryItem).filter(LibraryItem.id == "repro_mx1_item").update(
                    {"processed": True, "content": "the legacy worker's real extracted content"})
                db.commit()

                after_mx1 = refresh_item("repro_mx1_item")
                observe("row state immediately after the real old-code-shaped write, with the "
                        "continuous fencing trigger already installed",
                        (after_mx1.processed, after_mx1.entitlement_status))

                # An OLD backend reader's own query shape — this codebase's
                # CURRENT queries all correctly also check entitlement_status
                # (see is_source_unlocked); an old reader, by construction,
                # predates that column's meaning entirely and would only
                # ever have filtered on `processed`.
                old_reader_sees_it = (
                    db.query(LibraryItem)
                    .filter(LibraryItem.id == "repro_mx1_item", LibraryItem.processed.is_(True))
                    .first()
                ) is not None
                observe("would an OLD reader — filtering ONLY on processed=True — still find and "
                        "serve this item?", old_reader_sees_it)

                check(
                    "an old backend reader that knows only `processed` cannot serve/generate "
                    "source-derived content from an item the fencing mechanism has flagged — the "
                    "REAL old-reader query, executed here directly against the current database "
                    "state, must not still match the fenced item (correction #15: this harness "
                    "tests only mechanisms it can actually execute — database-state invisibility, "
                    "proven below — never an unexecuted, unmodeled 'external deployment drain' "
                    "claim)",
                    not old_reader_sees_it,
                    detail=(
                        f"entitlement_status={after_mx1.entitlement_status!r} — the continuous "
                        f"fencing trigger sets `entitlement_status = 'released'` but leaves "
                        f"`processed` UNCHANGED (True); every enforcement point THIS codebase adds "
                        f"(`is_source_unlocked`, `reconcile_unaccounted_processed_items`) correctly "
                        f"reads `entitlement_status` too, but a reader that predates that column's "
                        f"introduction — by definition unaware it needs to — would filter on "
                        f"`processed` alone and finds this item completely unchanged from its "
                        f"perspective; the fence is invisible to exactly the code it needs to stop"
                    ),
                )

                # ═════════════════════════════════════════════════════════
                section("MIXED-2 — SQLite trigger replacement: a stale installed definition must "
                        "be corrected, not preserved by `IF NOT EXISTS`")

                from sqlalchemy import text as _sqltext_m2

                TRIGGER_NAME_M2 = "trg_fence_unaccounted_processed_items_upd"
                with engine.connect() as conn_m2:
                    conn_m2.execute(_sqltext_m2(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME_M2}"))
                    # An intentionally WRONG/stale definition — no WHEN
                    # guard at all, and a deliberately-wrong target value —
                    # standing in for "whatever an earlier deployed version
                    # of this trigger happened to contain".
                    conn_m2.execute(_sqltext_m2(f"""
                        CREATE TRIGGER {TRIGGER_NAME_M2}
                        AFTER UPDATE ON library_items
                        BEGIN
                            UPDATE library_items SET entitlement_status = 'STALE_WRONG_VALUE' WHERE id = NEW.id;
                        END
                    """))
                    conn_m2.commit()

                sql_before_m2 = db.execute(_sqltext_m2(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:n"),
                    {"n": TRIGGER_NAME_M2}).scalar()
                observe("installed trigger SQL before re-running real initialization",
                        (sql_before_m2 or "")[:80])

                # Real production re-initialization — the exact function a
                # fresh boot calls.
                with engine.connect() as conn_m2b:
                    database_mod_local = __import__("app.database", fromlist=["_ensure_mixed_version_fencing"])
                    database_mod_local._ensure_mixed_version_fencing(conn_m2b)

                sql_after_m2 = db.execute(_sqltext_m2(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:n"),
                    {"n": TRIGGER_NAME_M2}).scalar()
                observe("installed trigger SQL after re-running real initialization",
                        (sql_after_m2 or "")[:80])

                check(
                    "re-running the real, current mixed-version-fencing initialization replaces a "
                    "stale/incorrect installed trigger definition with the current, correct one — "
                    "never preserves it",
                    sql_after_m2 is not None and "STALE_WRONG_VALUE" not in sql_after_m2,
                    detail=(
                        f"sql_after contains STALE_WRONG_VALUE="
                        f"{'STALE_WRONG_VALUE' in (sql_after_m2 or '')!r} — the SQLite branch uses "
                        f"`CREATE TRIGGER IF NOT EXISTS`, which is a genuine no-op whenever a "
                        f"trigger with that NAME already exists, regardless of what its actual SQL "
                        f"body says — a name collision with an outdated definition (from an earlier "
                        f"deploy, a manual hotfix, or this exact reproduction) is silently preserved "
                        f"forever, never corrected by any later boot"
                    ),
                )
                # Restore the real, correct trigger before continuing —
                # this section's own responsibility, not left for a later
                # section to discover broken.
                with engine.connect() as conn_m2d:
                    conn_m2d.execute(_sqltext_m2(f"DROP TRIGGER IF EXISTS {TRIGGER_NAME_M2}"))
                    conn_m2d.commit()
                with engine.connect() as conn_m2e:
                    database_mod_local._ensure_mixed_version_fencing(conn_m2e)

                # ═════════════════════════════════════════════════════════
                section("MIXED-3 — SQLite conditional corrective update: must not overwrite a "
                        "newer, already-accounted status")

                sql_m3 = db.execute(_sqltext_m2(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=:n"),
                    {"n": TRIGGER_NAME_M2}).scalar() or ""
                observe("the real, currently-installed corrective UPDATE statement's SQL text",
                        sql_m3)
                corrective_has_null_guard_m3 = (
                    "entitlement_status" in sql_m3
                    and "IS NULL" in sql_m3.split("UPDATE library_items")[-1]
                )
                check(
                    "[static] the SQLite corrective UPDATE's own WHERE clause retains an "
                    "`entitlement_status IS NULL` predicate, so it can never overwrite a row an "
                    "intervening accounting action already gave a real status",
                    corrective_has_null_guard_m3,
                    detail=(
                        f"trigger_sql={sql_m3!r} — the installed corrective statement is `UPDATE "
                        f"library_items SET entitlement_status = 'released' WHERE id = NEW.id`, "
                        f"with no predicate at all on the CURRENT value of `entitlement_status`"
                    ),
                )

                # Correction #16: the ORIGINAL version of this section
                # additionally ran `UPDATE library_items SET entitlement_
                # status = 'released' WHERE id = :id` directly against a
                # row real accounting had already resolved, and called the
                # resulting (correct-looking, since it happened to survive)
                # outcome a proof of "the trigger's own interleaving
                # behavior" — but that statement was executed by the TEST
                # itself, never by the real trigger, so it never actually
                # exercised any interleaving at all; it was a fabricated
                # substitute, exactly the pattern Hermes's audit correctly
                # rejected.
                #
                # REACHABILITY DETERMINATION (required by correction #16):
                # is the claimed interleaving — "another real accounting
                # action changes entitlement_status between the fencing
                # trigger's WHEN evaluation and its OWN corrective UPDATE"
                # — actually reachable under SQLite? No. SQLite's AFTER
                # UPDATE trigger body runs SYNCHRONOUSLY, as part of the
                # SAME statement/transaction that fired it, on the SAME
                # connection, before that statement returns control to any
                # other caller — and SQLite's own coarse, whole-database
                # writer lock means no OTHER connection's write can be
                # interleaved mid-transaction in the first place. There is
                # no real code path, on a single connection or across two,
                # by which a genuinely SEPARATE accounting write can land
                # strictly between this trigger's condition check and its
                # own corrective UPDATE. Since the dynamic race is not
                # reachable, it is removed entirely (not replaced with a
                # fabricated substitute) — the static SQL-shape assertion
                # above is retained on its own, honestly, as exactly what
                # it is: a structural code-quality observation about a
                # missing defensive predicate, not a proof of a reachable,
                # exploitable transition. See the completion report for
                # this same explanation.

                # ═════════════════════════════════════════════════════════
                section("MIXED-4 — real PostgreSQL trigger transition + real reconciliation sweep "
                        "(disposable local cluster)")

                # Correction #17/#18: uses the SAME shared, teardown-
                # disciplined `_disposable_postgres_cluster` helper Section
                # V uses; a real, PRODUCTION-COMPATIBLE schema (`Base.
                # metadata.create_all`, not a hand-picked 4-column table);
                # and — the key correction — the REAL `reconcile_
                # unaccounted_processed_items` sweep is genuinely executed
                # BEFORE the old-worker write, never replaced with a
                # comment/no-op.
                try:
                    with _disposable_postgres_cluster("nibbler-mixed4-pg-") as (pg_engine, pg_conn_kwargs):
                        Base.metadata.create_all(bind=pg_engine)
                        with pg_engine.connect() as pconn:
                            pconn.execute(_sqltext_m2(
                                "SELECT pg_advisory_lock(:k)"), {"k": database_mod_local.TASK2_ADVISORY_LOCK_KEY})
                            database_mod_local._ensure_mixed_version_fencing(pconn)
                            pconn.execute(_sqltext_m2(
                                "SELECT pg_advisory_unlock(:k)"), {"k": database_mod_local.TASK2_ADVISORY_LOCK_KEY})
                            pconn.commit()

                        from sqlalchemy.orm import sessionmaker as _sessionmaker_m4
                        PgSessionLocal_m4 = _sessionmaker_m4(bind=pg_engine)

                        # 1) existing row, processed=False, real ORM fixtures.
                        pg_session_m4 = PgSessionLocal_m4()
                        try:
                            pg_session_m4.add(User(id="mx4_u", email="mx4_u@example.com", created_at=OLD))
                            pg_session_m4.commit()
                            pg_session_m4.add(LibraryItem(id="mx4_item", user_id="mx4_u", type="pdf",
                                                            title="mx4_item", processed=False,
                                                            entitlement_status=None))
                            pg_session_m4.commit()

                            # 2) the REAL earlier reconciliation sweep —
                            # genuinely executed, not a comment/no-op —
                            # against this real Postgres database. With no
                            # unaccounted rows yet (this item is still
                            # processed=False), it should find nothing.
                            fixed_before_m4, failed_before_m4 = ent.reconcile_unaccounted_processed_items(
                                pg_session_m4)
                            observe("[real Postgres] real earlier reconciliation sweep result, "
                                    "before the old-worker write exists at all",
                                    (fixed_before_m4, failed_before_m4))
                            if failed_before_m4:
                                raise _PostgresCapabilityError(
                                    f"MIXED-4: the real earlier sweep itself failed against "
                                    f"real Postgres: {failed_before_m4} failure(s)")

                            # 3) the old-worker-style False→True write,
                            # AFTER that real sweep already ran and found
                            # nothing — exactly the mixed-version race this
                            # whole defect class is about.
                            pg_session_m4.query(LibraryItem).filter(LibraryItem.id == "mx4_item").update(
                                {"processed": True})
                            pg_session_m4.commit()
                        finally:
                            pg_session_m4.close()

                        # 4) do NOT re-run reconciliation — the assertion
                        # window this whole defect class is about.
                        verify_session_m4 = PgSessionLocal_m4()
                        try:
                            row_m4 = verify_session_m4.query(LibraryItem).filter(
                                LibraryItem.id == "mx4_item").first()
                            row_snapshot_m4 = (row_m4.processed, row_m4.entitlement_status)
                            observe("[real Postgres] row state immediately after the old-worker "
                                    "write, real earlier sweep already ran once, no reboot, no "
                                    "manual re-sweep", row_snapshot_m4)

                            check(
                                "[real Postgres] the trigger fences the row to 'released' "
                                "immediately, synchronously, within the SAME transaction as the "
                                "old-worker write — no reboot or manual sweep required, even "
                                "though the real earlier sweep already ran once and found nothing",
                                row_snapshot_m4[0] is True and row_snapshot_m4[1] == "released",
                                detail=f"row={row_snapshot_m4!r}",
                            )

                            # 5) the REAL old-reader query, executed as an
                            # actual assertion (never hard-coded to True).
                            old_reader_sees_pg = verify_session_m4.query(LibraryItem).filter(
                                LibraryItem.id == "mx4_item", LibraryItem.processed.is_(True)
                            ).first() is not None
                            check(
                                "[real Postgres] a real old-reader query (filtering ONLY on "
                                "processed=True, executed directly against this real database) "
                                "does not still match the fenced row",
                                not old_reader_sees_pg,
                                detail=f"old_reader_would_match={old_reader_sees_pg!r}",
                            )
                        finally:
                            verify_session_m4.close()

                        # 6) idempotent trigger installation — re-run
                        # again, confirm no error.
                        install_error_pg = None
                        try:
                            with pg_engine.connect() as pconn:
                                pconn.execute(_sqltext_m2(
                                    "SELECT pg_advisory_lock(:k)"), {"k": database_mod_local.TASK2_ADVISORY_LOCK_KEY})
                                database_mod_local._ensure_mixed_version_fencing(pconn)
                                pconn.execute(_sqltext_m2(
                                    "SELECT pg_advisory_unlock(:k)"), {"k": database_mod_local.TASK2_ADVISORY_LOCK_KEY})
                                pconn.commit()
                        except Exception as e:
                            install_error_pg = e
                        check(
                            "[real Postgres] the trigger's installation DDL is idempotent — safe "
                            "to run again against a database that already has it",
                            install_error_pg is None,
                            detail=f"install_error={install_error_pg!r}",
                        )
                except _PostgresCapabilityError as e:
                    check_capability(
                        "[real Postgres] a real disposable PostgreSQL cluster, real schema, and "
                        "the real reconciliation sweep could be exercised for MIXED-4",
                        False,
                        detail=f"{e}",
                    )

            # ═════════════════════════════════════════════════════════════
            run_section(_section_a, "A")
            run_section(_section_b, "B")
            run_section(_section_c, "C")
            run_section(_section_d, "D")
            run_section(_section_e, "E")
            run_section(_section_f, "F")
            run_section(_section_g, "G")
            run_section(_section_h, "H")
            run_section(_section_i, "I")
            run_section(_section_j, "J")
            run_section(_section_k, "K")
            run_section(_section_l, "L")
            run_section(_section_m, "M")
            run_section(_section_n, "N")
            run_section(_section_o, "O")
            run_section(_section_p, "P")
            run_section(_section_q, "Q")
            run_section(_section_r, "R")
            run_section(_section_s, "S")
            run_section(_section_t, "T")
            run_section(_section_u, "U")
            run_section(_section_v, "V")
            run_section(_section_w, "W")
            run_section(_section_x, "X")
            run_section(_section_y, "Y")
            run_section(_section_z, "Z")
            run_section(_section_mixed, "MIXED")

        finally:
            # Correction #20: explicit TestClient close + dependency_
            # overrides cleanup, unconditionally, regardless of how the
            # `try` block above exits.
            try:
                _client.close()
            except Exception:
                pass
            app_main.app.dependency_overrides.clear()
            db.close()
            try:
                engine.dispose()
            except Exception:
                pass

    # Correction #19: a blocked network attempt is a harness/environment
    # failure, never a product invariant — this file's OWN exit-code logic
    # must classify it as such (exit 2), even though `tests/hermetic.py`'s
    # own `atexit`-registered handler (which this assignment does not
    # permit modifying) separately forces the FINAL OS-level exit code to
    # 1 whenever any blocked attempt occurred, via `os._exit(1)` — a hard,
    # unconditional process-level exit that runs AFTER this function
    # returns and cannot be intercepted or overridden from here. This is a
    # genuine, disclosed limitation (see the completion report) — this
    # file's OWN internal classification is corrected below regardless.
    try:
        import hermetic as _hermetic_mod_final
        if getattr(_hermetic_mod_final, "blocked_attempts", None):
            harness_errors.append(
                f"{len(_hermetic_mod_final.blocked_attempts)} blocked outbound network "
                f"attempt(s) occurred during this run — a harness/environment failure, "
                f"classified as exit 2 by this file's own logic (see the completion report "
                f"for why the FINAL OS-level exit code may still show 1 — tests/hermetic.py's "
                f"own atexit handler is outside this assignment's authorized scope)"
            )
    except Exception:
        pass

    # Correction #20: prove no newly-created temp dir/thread/PG process
    # this run itself created survives it.
    hermetic_state_after = _snapshot_hermetic_state()
    residue_problems = _diff_hermetic_state(hermetic_state_before, hermetic_state_after)
    if residue_problems:
        for p in residue_problems:
            harness_errors.append(f"hermetic residue: {p}")

    # ═════════════════════════════════════════════════════════════════════
    print("\n" + "=" * 70)
    print(f"Product invariants PASSing:                {len(results_pass)}")
    print(f"Product invariants FAILing (current defects): {len(results_fail)}")
    for n in results_fail:
        print(f"    - {n}")
    print(f"Section E capability checks OK:             {len(capability_ok)}")
    print(f"Section E capability checks FAILED:         {len(capability_failed)}")
    for n in capability_failed:
        print(f"    - {n}")
    print(f"Harness/setup errors:                       {len(harness_errors)}")
    for n in harness_errors:
        print(f"    - {n}")

    exit_code = _decide_exit(results_fail, harness_errors)
    if exit_code == 2:
        print("\nRESULT: HARNESS/SETUP/CAPABILITY ERROR — see above. Not a clean invariant run.")
    elif exit_code == 1:
        print(f"\nRESULT: {len(results_fail)} product invariant(s) FAILED against current code, "
              f"as expected (RED). The same checks would PASS (exit 0) once production code is "
              f"fixed, with no edit to this file.")
    else:
        print("\nRESULT: every product invariant PASSED — current code already satisfies every "
              "invariant tested (GREEN).")
    return exit_code


def _run_selftest_exit_mode(mode):
    """2B-R1 correction #19: exercises the REAL `sys.exit(...)` process
    boundary with a synthetic result — invoked as a genuinely separate OS
    subprocess (see `_prove_real_process_exit_boundary` below), never a
    same-interpreter function call. `mode` is 'green' | 'red' | 'setup'.
    Deliberately short-circuits before any real setup (hermetic import,
    app import, database work) so each subprocess launch stays fast."""
    if mode == "green":
        sys.exit(_decide_exit([], []))
    elif mode == "red":
        sys.exit(_decide_exit(["synthetic product failure"], []))
    elif mode == "setup":
        sys.exit(_decide_exit([], ["synthetic setup/capability failure"]))
    else:
        sys.exit(3)  # unrecognized mode — never mistakeable for a real 0/1/2 result


def _prove_real_process_exit_boundary():
    """Launches THIS EXACT FILE as three genuinely separate OS
    subprocesses (`subprocess.run([sys.executable, __file__], env=...)`),
    each forcing a different synthetic result through `_run_selftest_exit_
    mode` via an env var, and checks each subprocess's REAL OS-level
    return code — not a Python-level function return value — matches
    0/1/2 exactly. This is what proves the actual `sys.exit(run_all())`
    boundary at the bottom of this file, not merely `_decide_exit`'s own
    pure logic in isolation. Returns (ok: bool, results: dict)."""
    import subprocess
    results = {}
    errors = {}
    for mode, expected in (("green", 0), ("red", 1), ("setup", 2)):
        env = dict(os.environ)
        env["NIBBLER_R1_SELFTEST_EXIT_MODE"] = mode
        try:
            proc = subprocess.run([sys.executable, os.path.abspath(__file__)],
                                   env=env, capture_output=True, timeout=30)
            results[mode] = (proc.returncode, expected)
        except Exception as e:
            errors[mode] = e
    ok = (not errors) and all(actual == expected for actual, expected in results.values())
    return ok, results, errors


if __name__ == "__main__":
    _selftest_mode = os.environ.get("NIBBLER_R1_SELFTEST_EXIT_MODE")
    if _selftest_mode:
        _run_selftest_exit_mode(_selftest_mode)
    sys.exit(run_all())
