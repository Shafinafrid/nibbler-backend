"""
tests/lifecycle/_runtime.py

Shared plain-Python test runtime for the future decomposed Task 2 lifecycle
suites: tri-state result classification (0/1/2) plus hermetic sandbox/
network ownership. This file owns NOTHING domain-specific — no SQLAlchemy
engine, no database, no FastAPI TestClient, no scheduler, no PostgreSQL
cluster, no mock, no Task 2 model row. Those belong to later, separately
authorized domain-specific fixtures built ON TOP of this file.

Import from any script living directly under tests/lifecycle/ as:

    import _runtime

This module MUST be imported before any `app` module. Import order inside
this file itself:
  1. standard-library modules only;
  2. locate tests/ from THIS FILE'S OWN __file__ (never the caller's cwd,
     never an assumed repo-root sys.path entry) and make it importable;
  3. import the existing tests/hermetic.py (unmodified — this file never
     edits it) as `hermetic`;
  4. replace hermetic's network-exit finalizer with an equivalent,
     exit-2-aware one, and set up sandbox ownership/reporting;
  5. only once all of the above has completed does control return to
     whatever script did `import _runtime` — at which point `app` modules
     become safely importable, because hermetic.py's own import already
     inserted the backend root onto sys.path and blanked every credential
     env var.

Every step above runs unconditionally at module-import time (nothing here
is deferred into a lazily-called setup function), so "import _runtime"
completing IS the guarantee that steps 1-4 already happened.
"""

import atexit
import json
import os
import shutil
import sys
import tempfile
import threading
import traceback

# ─── Steps 1-2: locate tests/ from OUR OWN __file__ ─────────────────────────
_LIFECYCLE_DIR = os.path.dirname(os.path.abspath(__file__))
TESTS_DIR = os.path.dirname(_LIFECYCLE_DIR)
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

# Captured BEFORE hermetic.py's own os.chdir(SANDBOX) runs (inside the
# `import hermetic` below), so controlled teardown can chdir back to
# somewhere real rather than guessing.
_ORIGINAL_CWD = os.getcwd()

# ─── Step 3: import the existing, unmodified tests/hermetic.py ─────────────
import hermetic  # noqa: E402  — must follow the sys.path setup above; not
                  # stdlib, but the whole point of steps 1-2 is making this
                  # importable without relying on an assumed sys.path entry.


# ─── Tri-state result collection ────────────────────────────────────────────

class RuntimeCollector:
    """File-level tri-state result collector. One instance per process,
    matching this repo's existing 'module-level failures list' convention
    (every test_task2_*.py script already does this), just split into the
    three buckets the tri-state contract requires instead of one.

      0 — no product failures and no runtime errors
      1 — at least one product failure, no runtime errors
      2 — at least one runtime/setup/capability/teardown error, regardless
          of product results (this category always wins)
    """

    def __init__(self):
        self.passed = []
        self.failed = []
        self.errors = []

    def merge_scenario(self, name, passed, failed, errors):
        self.passed.extend("%s: %s" % (name, p) for p in passed)
        self.failed.extend("%s: %s" % (name, f) for f in failed)
        self.errors.extend("%s: %s" % (name, e) for e in errors)

    def record_runtime_error(self, msg):
        self.errors.append(msg)

    def decide_exit(self):
        if self.errors:
            return 2
        if self.failed:
            return 1
        return 0

    def summary(self):
        return "PASS=%d FAIL=%d ERRORS=%d" % (
            len(self.passed), len(self.failed), len(self.errors))

    def finish_and_exit(self):
        """Prints a summary and exits the PROCESS with the decided code.
        This call itself only ever does `sys.exit(...)` — the actual real
        OS exit code is finalized by the atexit finalizer registered
        below, which preserves this code unless a blocked network attempt
        or a sandbox-cleanup failure requires overriding it to 2."""
        code = self.decide_exit()
        print("\n" + "=" * 62)
        print(self.summary())
        for e in self.errors:
            print("    [ERROR] %s" % e)
        for f in self.failed:
            print("    [FAIL] %s" % f)
        if code == 0:
            print("RESULT: every product invariant passed, no runtime errors")
        elif code == 1:
            print("RESULT: one or more product invariants FAILED (RED)")
        else:
            print("RESULT: a runtime/setup/capability/teardown error occurred")
        sys.exit(code)


RUNTIME = RuntimeCollector()


# ─── Generic scenario contract ──────────────────────────────────────────────

class Scenario:
    """A single, self-contained unit of work. Owns only generic
    setup/teardown/thread/resource-cleanup bookkeeping and its OWN local
    pass/fail/error lists, merged into the file-level RuntimeCollector only
    when the scenario closes. Deliberately does NOT know about SQLAlchemy,
    databases, FastAPI, schedulers, PostgreSQL, or mocks — a later,
    domain-specific fixture builds those on top of `add_setup`/
    `add_teardown`/`register_thread`, never inside this class.

    Usage:

        s = scenario("some real invariant")
        s.add_setup(build_something)      # runs during __enter__
        s.add_teardown(tear_it_down)       # runs during __exit__, always
        with s:
            if s.setup_failed:
                pass  # nothing meaningful to do; already a runtime error
            else:
                t = threading.Thread(target=..., daemon=True)
                t.start()
                s.register_thread(t)
                s.check("some real invariant holds", True)
    """

    def __init__(self, name, collector=None, join_timeout=5.0):
        self.name = name
        self.collector = collector if collector is not None else RUNTIME
        self.join_timeout = join_timeout
        self._setup_callbacks = []
        self._teardown_callbacks = []
        self._threads = []
        self._local_pass = []
        self._local_fail = []
        self._local_errors = []
        self.setup_failed = False
        self._entered = False

    # -- registration -------------------------------------------------------
    def add_setup(self, fn, label=None):
        """Registers a callback to run during __enter__, in registration
        order. Call BEFORE entering the `with` block (setup only makes
        sense before the scenario body runs)."""
        self._setup_callbacks.append((label or getattr(fn, "__name__", "setup"), fn))
        return self

    def add_teardown(self, fn, label=None):
        """Registers a callback that ALWAYS runs during __exit__, in
        registration order, regardless of whether setup failed, the body
        raised, or an earlier teardown callback itself raised. This is
        also the generic mechanism for any other owned-resource cleanup a
        later domain fixture needs (closing a file, removing a directory,
        disposing an engine, etc.) — there is no separate mechanism."""
        self._teardown_callbacks.append((label or getattr(fn, "__name__", "teardown"), fn))
        return self

    def register_thread(self, thread, label=None):
        """Registers an ALREADY-STARTED thread to be joined, with a
        bounded timeout, during __exit__. Requires a daemon thread: a
        non-daemon thread that never finishes would hang the whole
        process at interpreter shutdown even after this scenario has
        already, correctly, classified it as a runtime error — daemon
        threads are abandoned cleanly on process exit instead."""
        if not isinstance(thread, threading.Thread):
            raise TypeError("register_thread requires a threading.Thread")
        if not thread.daemon:
            raise ValueError(
                "scenario %r: register_thread requires a daemon thread "
                "(daemon=True) — see this method's docstring" % (self.name,)
            )
        self._threads.append((label or thread.name, thread))
        return thread

    # -- scenario-local result recording ------------------------------------
    def check(self, name, cond, detail=""):
        tag = "PASS" if cond else "FAIL"
        print("  [%s] %s%s" % (tag, name, ("  — %s" % detail) if detail else ""))
        (self._local_pass if cond else self._local_fail).append(name)
        return cond

    def runtime_error(self, msg):
        """Explicitly records a runtime/capability error from inside the
        scenario body — e.g. 'this invariant is not yet evaluable' — never
        counted as a product PASS or FAIL."""
        print("  [ERROR] %s" % msg)
        self._local_errors.append(msg)

    # -- context manager ------------------------------------------------------
    def __enter__(self):
        print("\n=== scenario: %s ===" % self.name)
        self._entered = True
        # `__enter__` catches and classifies setup failures ITSELF —
        # Python never calls `__exit__` when `__enter__` raises, so
        # letting a setup exception propagate here would silently skip
        # every teardown callback. Instead: catch, record, stop running
        # further setup, and return normally so `__exit__` still runs.
        for label, fn in self._setup_callbacks:
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — must catch everything
                self._local_errors.append(
                    "setup callback %r raised: %s: %s" % (label, type(e).__name__, e))
                traceback.print_exc()
                self.setup_failed = True
                break
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self._local_errors.append(
                "scenario body raised %s: %s" % (exc_type.__name__, exc))
            traceback.print_exc()

        # Every registered teardown callback is attempted, in order, even
        # if an earlier one raised — never short-circuited.
        for label, fn in self._teardown_callbacks:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                self._local_errors.append(
                    "teardown callback %r raised: %s: %s" % (label, type(e).__name__, e))
                traceback.print_exc()

        for label, th in self._threads:
            th.join(timeout=self.join_timeout)
            if th.is_alive():
                self._local_errors.append(
                    "thread %r did not terminate within the %.1fs bounded "
                    "join timeout" % (label, self.join_timeout)
                )

        self.collector.merge_scenario(
            self.name, self._local_pass, self._local_fail, self._local_errors)
        print("--- %s: pass=%d fail=%d errors=%d ---" % (
            self.name, len(self._local_pass), len(self._local_fail), len(self._local_errors)))
        # Suppress the body exception — it is already classified as a
        # runtime error above; letting it propagate would crash the whole
        # script and skip any LATER scenario / the final finish_and_exit().
        return True


def scenario(name, collector=None, join_timeout=5.0):
    return Scenario(name, collector=collector, join_timeout=join_timeout)


def finish_and_exit():
    RUNTIME.finish_and_exit()


# ─── Step 4a: sandbox ownership ─────────────────────────────────────────────

_cleanup_state = {"attempted": False, "ok": False, "error": None}


def _attempt_sandbox_cleanup():
    """Idempotent — safe to call more than once; only the first REAL
    attempt does anything, later calls just report the already-decided
    outcome. Changes cwd OUT of the sandbox first (removing a directory
    that is also the current working directory is fragile), removes the
    EXACT hermetic.SANDBOX path (never a prefix scan), and verifies."""
    if _cleanup_state["attempted"]:
        return _cleanup_state["ok"]
    _cleanup_state["attempted"] = True
    try:
        try:
            os.chdir(_ORIGINAL_CWD)
        except Exception:
            # The original cwd may itself be gone or unreachable in some
            # exotic teardown ordering — fall back to a location that is
            # guaranteed not to be inside the sandbox we are about to
            # remove, rather than leaving cwd pointed AT it.
            os.chdir(tempfile.gettempdir())
        if os.path.isdir(hermetic.SANDBOX):
            shutil.rmtree(hermetic.SANDBOX)
        removed = not os.path.isdir(hermetic.SANDBOX)
        _cleanup_state["ok"] = removed
        if not removed:
            _cleanup_state["error"] = "%r still exists after rmtree" % (hermetic.SANDBOX,)
        return removed
    except Exception as e:  # noqa: BLE001
        _cleanup_state["ok"] = False
        _cleanup_state["error"] = "%s: %s" % (type(e).__name__, e)
        return False


# ─── Step 4b: parent-supervised metadata reporting ──────────────────────────
# Immediately, before any `app` import (this whole file completes before
# returning control to a caller that would do that), report the exact
# sandbox path a parent runner would need to perform FALLBACK cleanup if
# this process terminates abnormally (os._exit, a kill signal) before its
# own finalizer below ever runs. The parent NEVER needs this for a normal
# exit — a normally-exiting child cleans its own sandbox itself.

_METADATA_PATH = os.environ.get("NIBBLER_RUNTIME_METADATA_PATH")


def _report_sandbox_to_parent():
    if not _METADATA_PATH:
        return
    if not os.path.isabs(_METADATA_PATH):
        RUNTIME.record_runtime_error(
            "NIBBLER_RUNTIME_METADATA_PATH must be an absolute path, got %r"
            % (_METADATA_PATH,)
        )
        return
    sandbox_real = os.path.realpath(hermetic.SANDBOX)
    metadata_real = os.path.realpath(_METADATA_PATH)
    if metadata_real == sandbox_real or metadata_real.startswith(sandbox_real + os.sep):
        RUNTIME.record_runtime_error(
            "NIBBLER_RUNTIME_METADATA_PATH (%r) must be outside hermetic.SANDBOX (%r)"
            % (_METADATA_PATH, hermetic.SANDBOX)
        )
        return
    try:
        with open(_METADATA_PATH, "w") as f:
            json.dump({"sandbox": hermetic.SANDBOX, "pid": os.getpid()}, f)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:  # noqa: BLE001
        RUNTIME.record_runtime_error(
            "failed to report runtime metadata to %r: %s: %s"
            % (_METADATA_PATH, type(e).__name__, e)
        )


_report_sandbox_to_parent()


# ─── Step 4c: hermetic finalizer replacement ────────────────────────────────
# `tests/hermetic.py` is never edited. Its OWN atexit-registered
# `_fail_on_network_use` unconditionally forces `os._exit(1)` on any
# blocked network attempt, which is indistinguishable, at the process
# level, from a genuine product-invariant failure. This unregisters that
# EXACT callback (by function-object identity — nothing else in
# hermetic.py is touched) and installs a semantically equivalent
# finalizer whose only difference is the exit CODE (2, "runtime/
# capability failure", not 1, "product failure") — the security property
# itself (a blocked attempt always forces a hard, non-catchable non-zero
# exit that no passing product assertion can hide) is fully preserved.

atexit.unregister(hermetic._fail_on_network_use)


def _finalize():
    """Ordering, per the runtime's finalizer contract:
      1. attempt sandbox cleanup;
      2. record cleanup failure if any;
      3. flush diagnostic output;
      4. blocked network OR cleanup failure -> hard os._exit(2);
      5. otherwise: preserve whatever exit code normal execution already
         chose (via RuntimeCollector.finish_and_exit()'s own sys.exit(...)).
    This function NEVER runs before sandbox cleanup has been attempted —
    unlike hermetic.py's original handler, which raced ahead of any
    cleanup this file performs, since it was the only atexit handler
    hermetic.py itself ever registered.
    """
    cleanup_ok = _attempt_sandbox_cleanup()
    if not cleanup_ok and _cleanup_state["error"]:
        # Recorded for completeness; the exit-code decision below no
        # longer depends on RUNTIME's own bucket, since a cleanup failure
        # forces the hard exit unconditionally regardless of what
        # RuntimeCollector.decide_exit() already returned.
        pass

    sys.stdout.flush()
    sys.stderr.flush()

    blocked = list(hermetic.blocked_attempts)
    if blocked or not cleanup_ok:
        if blocked:
            sys.stderr.write(
                "\n[_runtime] HERMETIC FAILURE: %d blocked outbound connection "
                "attempt(s) during this run:\n" % len(blocked)
            )
            for addr in blocked[:10]:
                sys.stderr.write("  NETWORK_ATTEMPT_BLOCKED %r\n" % (addr,))
            sys.stderr.write("Tests must not contact external services. Stub the client.\n")
        if not cleanup_ok:
            sys.stderr.write(
                "\n[_runtime] SANDBOX CLEANUP FAILURE: %s\n" % (_cleanup_state["error"],)
            )
        sys.stderr.flush()
        os._exit(2)
    # otherwise: fall through, letting whatever exit code normal execution
    # already selected (via sys.exit(...) inside finish_and_exit()) stand.


atexit.register(_finalize)
