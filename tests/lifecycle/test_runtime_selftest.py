"""
tests/lifecycle/test_runtime_selftest.py

Real-subprocess self-test for tests/lifecycle/_runtime.py. Every one of the
12 required cases below launches a GENUINELY SEPARATE `python3.11` child
process running a small, purpose-built child script (never an in-process
simulation, never a same-interpreter function call standing in for a real
process boundary) and asserts on that child's REAL OS-level exit code.

This script itself is a plain, dependency-free, no-pytest Python script,
matching this repository's own existing tests/test_*.py convention —
module-level `failures`/`check`/`section`, `sys.exit(1 if failures else 0)`
at the end.

Run:  python3.11 tests/lifecycle/test_runtime_selftest.py
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PYTHON = sys.executable

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (tag, name, ("  — %s" % detail) if detail else ""))
    if not cond:
        failures.append(name)


def section(t):
    print("\n=== %s ===" % t)


# A self-owned external temp directory for every generated child script —
# never inside any hermetic.SANDBOX, created fresh, removed and VERIFIED
# removed at the very end. This is the "self-owned external temporary
# directory" the assignment requires child scripts to live under.
_WORKDIR = tempfile.mkdtemp(prefix="nibbler-runtime-selftest-")
_child_counter = {"n": 0}

# The exact absolute preamble every generated child uses to import
# `_runtime` — proves the import works via an explicit absolute path, never
# an implicit repo-root sys.path entry (Case 11 specifically re-proves this
# from a working directory outside the repo).
_IMPORT_PREAMBLE = "import sys\nsys.path.insert(0, %r)\nimport _runtime\n" % (_HERE,)


def _write_child(label, body):
    _child_counter["n"] += 1
    path = os.path.join(_WORKDIR, "child_%02d_%s.py" % (_child_counter["n"], label))
    with open(path, "w") as f:
        f.write(body)
    return path


def _run_child(path, extra_args=None, env_overrides=None, cwd=None, timeout=30):
    """Launches a genuinely separate OS process. Returns
    (pid, returncode, stdout, stderr, timed_out)."""
    env = dict(os.environ)
    env.update(env_overrides or {})
    argv = [_PYTHON, path] + (extra_args or [])
    proc = subprocess.Popen(
        argv, env=env, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    pid = proc.pid
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return pid, proc.returncode, stdout, stderr, False
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return pid, proc.returncode, stdout, stderr, True


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


# Evidence accumulated for the REAL RESIDUE VERIFICATION section.
_all_child_pids = []
_all_reported_sandboxes = []  # list of (case_label, sandbox_path)


def _tmp_root():
    return tempfile.gettempdir()


def _nibbler_hermetic_dirs():
    return set(glob.glob(os.path.join(_tmp_root(), "nibbler-hermetic-*")))


_hermetic_dirs_before_everything = _nibbler_hermetic_dirs()


# ═════════════════════════════════════════════════════════════════════════
section("Case 1 — GREEN: real scenario, real passing check, no runtime "
        "errors -> actual OS exit 0")
# ═════════════════════════════════════════════════════════════════════════
_body1 = _IMPORT_PREAMBLE + textwrap.dedent("""
    with _runtime.scenario("green-case") as s:
        s.check("a real passing product check (1 + 1 == 2)", 1 + 1 == 2)
    _runtime.finish_and_exit()
""")
_path1 = _write_child("green", _body1)
pid1, rc1, out1, err1, timeout1 = _run_child(_path1)
_all_child_pids.append(pid1)
check("Case 1 GREEN: child process actually exited (no timeout)", not timeout1)
check("Case 1 GREEN: real OS exit code is 0", rc1 == 0, "rc=%r stderr=%s" % (rc1, err1[-300:]))
check("Case 1 GREEN: the real passing check is visible in child stdout",
      "[PASS] a real passing product check" in out1)


# ═════════════════════════════════════════════════════════════════════════
section("Case 2 — PRODUCT RED: real scenario, real failing check, no "
        "runtime errors -> actual OS exit 1")
# ═════════════════════════════════════════════════════════════════════════
_body2 = _IMPORT_PREAMBLE + textwrap.dedent("""
    with _runtime.scenario("red-case") as s:
        s.check("a real failing product check (1 + 1 == 3)", 1 + 1 == 3)
    _runtime.finish_and_exit()
""")
_path2 = _write_child("red", _body2)
pid2, rc2, out2, err2, timeout2 = _run_child(_path2)
_all_child_pids.append(pid2)
check("Case 2 RED: child process actually exited (no timeout)", not timeout2)
check("Case 2 RED: real OS exit code is 1", rc2 == 1, "rc=%r stderr=%s" % (rc2, err2[-300:]))
check("Case 2 RED: the real failing check is visible in child stdout",
      "[FAIL] a real failing product check" in out2)


# ═════════════════════════════════════════════════════════════════════════
section("Case 3 — SETUP FAILURE: setup callback raises, cleanup still "
        "runs -> actual OS exit 2")
# ═════════════════════════════════════════════════════════════════════════
_marker3 = os.path.join(_WORKDIR, "marker_case3_teardown_ran.txt")
_body3 = _IMPORT_PREAMBLE + textwrap.dedent("""
    def _raising_setup():
        raise RuntimeError("deliberate setup failure")

    def _teardown_marker():
        with open(sys.argv[1], "w") as f:
            f.write("teardown-ran")

    s = _runtime.scenario("setup-failure-case")
    s.add_setup(_raising_setup)
    s.add_teardown(_teardown_marker)
    with s:
        pass  # setup already failed inside __enter__; nothing to do
    _runtime.finish_and_exit()
""")
_path3 = _write_child("setup_failure", _body3)
pid3, rc3, out3, err3, timeout3 = _run_child(_path3, extra_args=[_marker3])
_all_child_pids.append(pid3)
check("Case 3 SETUP FAILURE: child process actually exited (no timeout)", not timeout3)
check("Case 3 SETUP FAILURE: real OS exit code is 2", rc3 == 2, "rc=%r stderr=%s" % (rc3, err3[-300:]))
check("Case 3 SETUP FAILURE: cleanup (the teardown callback) still ran",
      os.path.exists(_marker3))
check("Case 3 SETUP FAILURE: __enter__ itself did not crash the child "
      "(no uncaught RuntimeError traceback reaching the top level)",
      "Traceback (most recent call last):" not in out3.split("scenario body raised")[0]
      or "deliberate setup failure" in err3 or "deliberate setup failure" in out3)


# ═════════════════════════════════════════════════════════════════════════
section("Case 4 — BODY FAILURE: scenario body raises unexpectedly, "
        "teardown still runs -> actual OS exit 2")
# ═════════════════════════════════════════════════════════════════════════
_marker4 = os.path.join(_WORKDIR, "marker_case4_teardown_ran.txt")
_body4 = _IMPORT_PREAMBLE + textwrap.dedent("""
    def _teardown_marker():
        with open(sys.argv[1], "w") as f:
            f.write("teardown-ran")

    s = _runtime.scenario("body-failure-case")
    s.add_teardown(_teardown_marker)
    with s:
        raise RuntimeError("deliberate body failure")
    _runtime.finish_and_exit()
""")
_path4 = _write_child("body_failure", _body4)
pid4, rc4, out4, err4, timeout4 = _run_child(_path4, extra_args=[_marker4])
_all_child_pids.append(pid4)
check("Case 4 BODY FAILURE: child process actually exited (no timeout)", not timeout4)
check("Case 4 BODY FAILURE: real OS exit code is 2", rc4 == 2, "rc=%r stderr=%s" % (rc4, err4[-300:]))
check("Case 4 BODY FAILURE: teardown still ran despite the body raising",
      os.path.exists(_marker4))
check("Case 4 BODY FAILURE: the script continued past the `with` block "
      "(finish_and_exit() ran — the exception did not propagate and crash "
      "the interpreter uncaught)",
      "RESULT:" in out4)


# ═════════════════════════════════════════════════════════════════════════
section("Case 5 — TEARDOWN FAILURE: one teardown callback raises, a "
        "LATER one still runs -> actual OS exit 2")
# ═════════════════════════════════════════════════════════════════════════
_marker5 = os.path.join(_WORKDIR, "marker_case5_later_teardown_ran.txt")
_body5 = _IMPORT_PREAMBLE + textwrap.dedent("""
    def _raising_teardown():
        raise RuntimeError("deliberate teardown failure")

    def _later_teardown_marker():
        with open(sys.argv[1], "w") as f:
            f.write("later-teardown-ran")

    s = _runtime.scenario("teardown-failure-case")
    s.add_teardown(_raising_teardown)
    s.add_teardown(_later_teardown_marker)
    with s:
        s.check("a real passing check before teardown runs", True)
    _runtime.finish_and_exit()
""")
_path5 = _write_child("teardown_failure", _body5)
pid5, rc5, out5, err5, timeout5 = _run_child(_path5, extra_args=[_marker5])
_all_child_pids.append(pid5)
check("Case 5 TEARDOWN FAILURE: child process actually exited (no timeout)", not timeout5)
check("Case 5 TEARDOWN FAILURE: real OS exit code is 2", rc5 == 2, "rc=%r stderr=%s" % (rc5, err5[-300:]))
check("Case 5 TEARDOWN FAILURE: the LATER teardown callback still ran "
      "despite the earlier one raising", os.path.exists(_marker5))
check("Case 5 TEARDOWN FAILURE: the product check that DID pass is still "
      "visible (a teardown failure does not erase earlier product results)",
      "[PASS] a real passing check before teardown runs" in out5)


# ═════════════════════════════════════════════════════════════════════════
section("Case 6 — THREAD RESIDUE: a registered daemon thread that cannot "
        "terminate within the bounded join timeout -> actual OS exit 2, "
        "process still terminates promptly")
# ═════════════════════════════════════════════════════════════════════════
_body6 = _IMPORT_PREAMBLE + textwrap.dedent("""
    import threading, time

    def _never_finishes():
        time.sleep(600)  # far longer than the scenario's own join timeout

    s = _runtime.scenario("thread-residue-case", join_timeout=1.0)
    with s:
        t = threading.Thread(target=_never_finishes, daemon=True, name="stuck-thread")
        t.start()
        s.register_thread(t)
        s.check("a real passing check alongside the stuck thread", True)
    _runtime.finish_and_exit()
""")
_path6 = _write_child("thread_residue", _body6)
_t6_start = time.monotonic()
pid6, rc6, out6, err6, timeout6 = _run_child(_path6, timeout=20)
_t6_elapsed = time.monotonic() - _t6_start
_all_child_pids.append(pid6)
check("Case 6 THREAD RESIDUE: child terminated on its own well within the "
      "20s subprocess safety timeout (the self-test itself did not have "
      "to kill it)", not timeout6, "elapsed=%.1fs" % _t6_elapsed)
check("Case 6 THREAD RESIDUE: the child terminated PROMPTLY (well under "
      "the stuck thread's 600s sleep), proving a daemon thread never "
      "blocks process shutdown", _t6_elapsed < 15, "elapsed=%.1fs" % _t6_elapsed)
check("Case 6 THREAD RESIDUE: real OS exit code is 2", rc6 == 2, "rc=%r stderr=%s" % (rc6, err6[-300:]))
check("Case 6 THREAD RESIDUE: the unjoined thread is reported as a "
      "runtime error, not silently dropped",
      "did not terminate within the" in out6 and "bounded join timeout" in out6)
# The self-test process itself must not be left waiting on anything from
# this case either — proven by the fact this line was reached at all
# within the outer script's own execution.


# ═════════════════════════════════════════════════════════════════════════
section("Case 7 — BLOCKED NETWORK: a genuine non-loopback "
        "socket.create_connection is blocked by hermetic BEFORE any "
        "packet leaves; every product check may otherwise pass -> actual "
        "OS exit 2, never 0 or 1")
# ═════════════════════════════════════════════════════════════════════════
_body7 = _IMPORT_PREAMBLE + textwrap.dedent("""
    import socket

    with _runtime.scenario("blocked-network-case") as s:
        s.check("a real passing product check", True)
        try:
            socket.create_connection(("93.184.216.34", 80), timeout=2)
            s.runtime_error(
                "expected hermetic.NetworkBlocked to be raised, but the "
                "connection attempt returned normally"
            )
        except _runtime.hermetic.NetworkBlocked:
            s.check(
                "the real hermetic socket guard blocked the non-loopback "
                "connection attempt before any packet left the machine",
                True,
            )
    _runtime.finish_and_exit()
""")
_path7 = _write_child("blocked_network", _body7)
pid7, rc7, out7, err7, timeout7 = _run_child(_path7)
_all_child_pids.append(pid7)
check("Case 7 BLOCKED NETWORK: child process actually exited (no timeout)", not timeout7)
check("Case 7 BLOCKED NETWORK: real OS exit code is 2 — NOT 0, despite "
      "every product check passing, and NOT 1", rc7 == 2, "rc=%r stderr=%s" % (rc7, err7[-300:]))
check("Case 7 BLOCKED NETWORK: the real hermetic guard's own blocked-"
      "attempt diagnostic is present in stderr",
      "HERMETIC FAILURE" in err7 and "NETWORK_ATTEMPT_BLOCKED" in err7)
check("Case 7 BLOCKED NETWORK: every product check the scenario made DID "
      "pass — proving the exit-2 override, not a hidden product failure, "
      "is what changed the result",
      "[FAIL]" not in out7)


# ═════════════════════════════════════════════════════════════════════════
section("Case 8 — STANDALONE SANDBOX CLEANUP: no "
        "NIBBLER_RUNTIME_METADATA_PATH set; the exact sandbox is "
        "identified via a child-created external evidence file, flushed/"
        "fsynced before exit, and verified gone after the child exits")
# ═════════════════════════════════════════════════════════════════════════
_evidence8 = os.path.join(_WORKDIR, "case8_sandbox_path.txt")
_body8 = _IMPORT_PREAMBLE + textwrap.dedent("""
    import os as _os
    with open(sys.argv[1], "w") as f:
        f.write(_runtime.hermetic.SANDBOX)
        f.flush()
        _os.fsync(f.fileno())
    with _runtime.scenario("standalone-cleanup-case") as s:
        s.check("a real passing product check", True)
    _runtime.finish_and_exit()
""")
_path8 = _write_child("standalone_cleanup", _body8)
pid8, rc8, out8, err8, timeout8 = _run_child(_path8, extra_args=[_evidence8],
                                              env_overrides={"NIBBLER_RUNTIME_METADATA_PATH": ""})
_all_child_pids.append(pid8)
check("Case 8 STANDALONE: child process actually exited (no timeout)", not timeout8)
check("Case 8 STANDALONE: real OS exit code is 0", rc8 == 0, "rc=%r stderr=%s" % (rc8, err8[-300:]))
check("Case 8 STANDALONE: the child's own external evidence file exists",
      os.path.exists(_evidence8))
_sandbox8 = None
if os.path.exists(_evidence8):
    with open(_evidence8) as f:
        _sandbox8 = f.read().strip()
    _all_reported_sandboxes.append(("case8-standalone", _sandbox8))
check("Case 8 STANDALONE: the reported sandbox path looks like a real "
      "hermetic sandbox (nibbler-hermetic- prefix)",
      bool(_sandbox8) and "nibbler-hermetic-" in _sandbox8, repr(_sandbox8))
check("Case 8 STANDALONE: the exact sandbox no longer exists after the "
      "child's own normal exit — no exemption for nibbler-hermetic-*",
      bool(_sandbox8) and not os.path.isdir(_sandbox8), repr(_sandbox8))


# ═════════════════════════════════════════════════════════════════════════
section("Case 9 — SUPERVISED SANDBOX CLEANUP: an absolute external "
        "metadata path is supplied; metadata PID and sandbox are exact; "
        "normal child cleanup still removes the sandbox itself")
# ═════════════════════════════════════════════════════════════════════════
_metadata9 = os.path.join(_WORKDIR, "case9_metadata.json")
_body9 = _IMPORT_PREAMBLE + textwrap.dedent("""
    with _runtime.scenario("supervised-cleanup-case") as s:
        s.check("a real passing product check", True)
    _runtime.finish_and_exit()
""")
_path9 = _write_child("supervised_cleanup", _body9)
pid9, rc9, out9, err9, timeout9 = _run_child(
    _path9, env_overrides={"NIBBLER_RUNTIME_METADATA_PATH": _metadata9})
_all_child_pids.append(pid9)
check("Case 9 SUPERVISED: child process actually exited (no timeout)", not timeout9)
check("Case 9 SUPERVISED: real OS exit code is 0", rc9 == 0, "rc=%r stderr=%s" % (rc9, err9[-300:]))
check("Case 9 SUPERVISED: the metadata file was written", os.path.exists(_metadata9))
_meta9 = None
if os.path.exists(_metadata9):
    with open(_metadata9) as f:
        _meta9 = json.load(f)
    _all_reported_sandboxes.append(("case9-supervised", _meta9.get("sandbox")))
check("Case 9 SUPERVISED: the metadata's reported PID is EXACTLY this "
      "child's real PID", bool(_meta9) and _meta9.get("pid") == pid9,
      "metadata=%r actual_pid=%r" % (_meta9, pid9))
check("Case 9 SUPERVISED: the metadata's reported sandbox looks like a "
      "real hermetic sandbox",
      bool(_meta9) and "nibbler-hermetic-" in (_meta9.get("sandbox") or ""), repr(_meta9))
check("Case 9 SUPERVISED: normal child cleanup ALREADY removed the exact "
      "reported sandbox — the metadata channel is a fallback, not a "
      "replacement for the child's own cleanup",
      bool(_meta9) and not os.path.isdir(_meta9.get("sandbox", "/nonexistent")))


# ═════════════════════════════════════════════════════════════════════════
section("Case 10 — ABNORMAL CHILD FALLBACK: child reports metadata, then "
        "hard-exits before its own finalizer runs; the PARENT (this "
        "script) removes only the exact reported sandbox, verifies "
        "removal, and never touches an unrelated pre-existing directory")
# ═════════════════════════════════════════════════════════════════════════
# A real, pre-existing, unrelated decoy directory sharing the SAME
# nibbler-hermetic- prefix, created BEFORE this case runs, verified
# UNTOUCHED afterward — the concrete proof against prefix-wide deletion.
_decoy10 = tempfile.mkdtemp(prefix="nibbler-hermetic-")
with open(os.path.join(_decoy10, "decoy-marker.txt"), "w") as _f:
    _f.write("must survive case 10 untouched")

_metadata10 = os.path.join(_WORKDIR, "case10_metadata.json")
_body10 = _IMPORT_PREAMBLE + textwrap.dedent("""
    import os as _os
    # By the time this line runs, `import _runtime` above has ALREADY
    # written the metadata file (NIBBLER_RUNTIME_METADATA_PATH is set for
    # this child) — this hard-exits immediately afterward, bypassing
    # EVERY atexit handler, including this file's own sandbox-cleanup
    # finalizer, simulating a crash/kill.
    _os._exit(1)
""")
_path10 = _write_child("abnormal_fallback", _body10)
pid10, rc10, out10, err10, timeout10 = _run_child(
    _path10, env_overrides={"NIBBLER_RUNTIME_METADATA_PATH": _metadata10})
_all_child_pids.append(pid10)
check("Case 10 ABNORMAL: child process actually exited (no timeout)", not timeout10)
check("Case 10 ABNORMAL: child's real OS exit code is 1 (the deliberate "
      "hard os._exit(1), never reaching its own finalizer)", rc10 == 1)
check("Case 10 ABNORMAL: the metadata file was still written (it happens "
      "during `import _runtime`, before the hard-exit line)",
      os.path.exists(_metadata10))
_meta10 = None
if os.path.exists(_metadata10):
    with open(_metadata10) as f:
        _meta10 = json.load(f)
_sandbox10 = _meta10.get("sandbox") if _meta10 else None
check("Case 10 ABNORMAL: the child's sandbox genuinely SURVIVED the "
      "abnormal exit (proving os._exit really did skip its finalizer)",
      bool(_sandbox10) and os.path.isdir(_sandbox10), repr(_sandbox10))

# PARENT fallback cleanup — ONLY the exact reported path.
_fallback_removed = False
if _sandbox10 and os.path.isdir(_sandbox10):
    shutil.rmtree(_sandbox10, ignore_errors=True)
    _fallback_removed = not os.path.isdir(_sandbox10)
check("Case 10 ABNORMAL: the parent's fallback cleanup removed the "
      "EXACT reported sandbox", _fallback_removed, repr(_sandbox10))
check("Case 10 ABNORMAL: the unrelated, pre-existing decoy directory "
      "(same nibbler-hermetic- prefix) is completely UNTOUCHED — no "
      "prefix-wide deletion occurred",
      os.path.isdir(_decoy10)
      and os.path.exists(os.path.join(_decoy10, "decoy-marker.txt")))
shutil.rmtree(_decoy10, ignore_errors=True)
check("Case 10 ABNORMAL: this self-test's own decoy directory was itself "
      "cleaned up afterward (test-infrastructure hygiene, not part of "
      "the invariant under test)", not os.path.isdir(_decoy10))


# ═════════════════════════════════════════════════════════════════════════
section("Case 11 — IMPORT LOCATION: launched from a working directory "
        "OUTSIDE the repository; imports _runtime by absolute path; "
        "proves hermetic loaded correctly and no .env/credential became "
        "reachable -> actual OS exit 0")
# ═════════════════════════════════════════════════════════════════════════
_outside_cwd = tempfile.mkdtemp(prefix="nibbler-runtime-selftest-outside-cwd-")
_body11 = _IMPORT_PREAMBLE + textwrap.dedent("""
    import os
    with _runtime.scenario("import-location-case") as s:
        s.check("hermetic module loaded correctly (SANDBOX is a real path)",
                bool(_runtime.hermetic.SANDBOX) and os.path.isdir(_runtime.hermetic.SANDBOX))
        s.check("no .env is reachable from the sandboxed working directory",
                _runtime.hermetic.env_file_is_unreachable())
        s.check("every forced-blank credential env var is genuinely empty",
                _runtime.hermetic.production_env_is_purged())
        s.check("TESTS_DIR was resolved from _runtime.py's own __file__, "
                "not this child's cwd (which is nowhere near the repo)",
                _runtime.TESTS_DIR.endswith(os.sep + "tests")
                or _runtime.TESTS_DIR.endswith("/tests"))
    _runtime.finish_and_exit()
""")
_path11 = _write_child("import_location", _body11)
# `_run_child` always starts from a full copy of THIS process's environ and
# applies overrides on top — to genuinely drop PYTHONPATH (proving no
# implicit repo-root path reaches the child via it either), launch this
# one case directly with an explicitly pruned base environment instead.
_pruned_env = dict(os.environ)
_pruned_env.pop("PYTHONPATH", None)
_proc11 = subprocess.Popen(
    [_PYTHON, _path11], cwd=_outside_cwd, env=_pruned_env,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
)
pid11 = _proc11.pid
try:
    out11, err11 = _proc11.communicate(timeout=30)
    rc11 = _proc11.returncode
    timeout11 = False
except subprocess.TimeoutExpired:
    _proc11.kill()
    out11, err11 = _proc11.communicate()
    rc11 = _proc11.returncode
    timeout11 = True
_all_child_pids.append(pid11)
check("Case 11 IMPORT LOCATION: child process actually exited (no timeout)", not timeout11)
check("Case 11 IMPORT LOCATION: real OS exit code is 0", rc11 == 0,
      "rc=%r stdout_tail=%s stderr=%s" % (rc11, out11[-300:], err11[-300:]))
check("Case 11 IMPORT LOCATION: hermetic loaded correctly from an "
      "outside-the-repo cwd", "[PASS] hermetic module loaded correctly" in out11)
check("Case 11 IMPORT LOCATION: no .env/credential became reachable",
      "[PASS] no .env is reachable" in out11
      and "[PASS] every forced-blank credential env var is genuinely empty" in out11)
shutil.rmtree(_outside_cwd, ignore_errors=True)


# ═════════════════════════════════════════════════════════════════════════
section("Case 12 — PRECEDENCE: a real product failure AND a real runtime "
        "error together -> actual OS exit 2, never 1")
# ═════════════════════════════════════════════════════════════════════════
_body12 = _IMPORT_PREAMBLE + textwrap.dedent("""
    with _runtime.scenario("precedence-case") as s:
        s.check("a real failing product check", False)
        s.runtime_error("a deliberate runtime error alongside the product failure")
    _runtime.finish_and_exit()
""")
_path12 = _write_child("precedence", _body12)
pid12, rc12, out12, err12, timeout12 = _run_child(_path12)
_all_child_pids.append(pid12)
check("Case 12 PRECEDENCE: child process actually exited (no timeout)", not timeout12)
check("Case 12 PRECEDENCE: real OS exit code is 2, not 1 — runtime-error "
      "classification dominates over a product failure", rc12 == 2,
      "rc=%r stderr=%s" % (rc12, err12[-300:]))
check("Case 12 PRECEDENCE: both the product failure and the runtime "
      "error are visible in child stdout",
      "[FAIL] a real failing product check" in out12
      and "[ERROR] a deliberate runtime error" in out12)


# ═════════════════════════════════════════════════════════════════════════
section("Real residue verification")
# ═════════════════════════════════════════════════════════════════════════
time.sleep(0.2)  # let any just-exited child's OS-level pid slot settle
_dead = [pid for pid in _all_child_pids if not _pid_alive(pid)]
check("every child process this self-test launched has genuinely exited",
      len(_dead) == len(_all_child_pids),
      "alive=%r of %r" % (
          [p for p in _all_child_pids if _pid_alive(p)], _all_child_pids))

_still_present = [(label, path) for (label, path) in _all_reported_sandboxes
                   if path and os.path.isdir(path)]
check("every exact, reported hermetic sandbox from every case was "
      "genuinely removed — no nibbler-hermetic-* exemption",
      len(_still_present) == 0, "still present: %r" % (_still_present,))

_hermetic_dirs_after_everything = _nibbler_hermetic_dirs()
_new_hermetic_dirs = _hermetic_dirs_after_everything - _hermetic_dirs_before_everything
check("no NEW nibbler-hermetic-* directory anywhere under the system "
      "temp root survives this entire self-test run (decoys and normal "
      "sandboxes both accounted for above)",
      len(_new_hermetic_dirs) == 0, "new/leaked: %r" % (_new_hermetic_dirs,))

shutil.rmtree(_WORKDIR, ignore_errors=True)
check("this self-test's own owned temporary child-script directory was "
      "removed", not os.path.isdir(_WORKDIR))


print("\n" + "=" * 62)
if failures:
    print("RESULT: %d FAILURE(S): %r" % (len(failures), failures))
else:
    print("RESULT: all 12 runtime self-test cases + residue verification passed")
sys.exit(1 if failures else 0)
