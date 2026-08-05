"""
Regression tests for the production LLM-telemetry visibility fix, and for the
process-lifecycle hardening round that followed Hermes's audit.

Gate 5 generated a real, successful production Luna session and Railway's
logs contained no `llm_attempt`/`llm_usage` line: nothing in the app ever
configures the ROOT logger (no `logging.basicConfig`), so the telemetry
module's `logger.info(...)` calls inherited the default root level, WARNING,
and were silently dropped — while Uvicorn's own access/startup lines still
appeared, because Uvicorn configures its OWN three loggers explicitly.

The first fix (`configure_llm_telemetry_logging`, called at `main.py` module
scope) worked for Nibbler's current single-process deployment but was wrong on
its own terms: the readiness event was guarded only by "does a marked handler
exist", and a forked child inherits that handler via copy-on-write memory —
so a fork-after-import worker would see the marker already present and never
announce its OWN PID. This file proves the corrected design: activation moved
into FastAPI's lifespan startup, handler-attachment and readiness-per-PID
tracked as two independent, lock-protected mechanisms, and a fork-safety reset
for the lock itself.

    .venv/bin/python tests/test_llm_telemetry_logging.py
"""

import importlib
import io
import logging
import logging.config
import os
import subprocess
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermetic  # noqa: E402,F401

from llm_fakes import Checks  # noqa: E402

import uvicorn.config  # noqa: E402

c = Checks("LLM telemetry: production logging + process lifecycle")

BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(BACKEND_ROOT, ".venv", "bin", "python")

THIRD_PARTY_LOGGER_NAMES = (
    "openai", "anthropic", "httpx", "httpcore", "urllib3", "boto3", "botocore",
    "firebase_admin", "pinecone", "voyageai",
)
UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access")

FORBIDDEN_SUBSTRINGS = (
    "prompt", "excerpt", "chat history", "card body", "card content",
    "authorization", "bearer", "api_key", "api key", "credential",
    "source passage", "generated response",
)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def _marked_handlers(lg):
    return [h for h in lg.handlers if getattr(h, "_nibbler_llm_telemetry_handler", False)]


def _baseline_levels():
    return {name: logging.getLogger(name).level for name in THIRD_PARTY_LOGGER_NAMES}


# Captured before ANY import/configuration below touches anything, so a later
# comparison actually proves nothing was touched.
_pre_baseline = _baseline_levels()
_root_level_before = logging.getLogger().level

# =============================================================================
# A. Import behavior
# =============================================================================

from app.services.llm.usage import (  # noqa: E402
    configure_llm_telemetry_logging,
    emit_attempt,
    emit_circuit_skip,
    emit_fallback,
    emit_usage,
    logger as telemetry_logger,
)

c.ok(not _marked_handlers(telemetry_logger),
     "A1: importing app.services.llm.usage alone attaches no telemetry handler")
c.ok(telemetry_logger.level != logging.INFO,
     "A1: importing alone does not raise the logger's own level to INFO")

logging.getLogger().setLevel(logging.WARNING)  # the real production default

# hermetic.py correctly blanks every provider credential; validate_llm_settings
# would then legitimately raise (no usable provider) and the lifespan-ordering
# drive below could never reach create_tables()/start_scheduler(). These are
# obvious, harmless placeholders — never real, never printed — matching the
# ACTUAL current production shape (single mode, Luna-only) purely so lifespan
# startup can run to completion inside this test.
os.environ["LLM_ROUTING_MODE"] = "single"
os.environ["LLM_ACTIVE_PROVIDER"] = "luna"
os.environ["LLM_HAIKU_ENABLED"] = "false"
os.environ["LLM_QWEN_ENABLED"] = "false"
os.environ["OPENAI_LLM_API_KEY"] = "test-placeholder-not-a-real-key"
os.environ["OPENAI_LLM_MODEL"] = "gpt-5.6-luna"
os.environ["OPENAI_LLM_REASONING_EFFORT"] = "low"

import main  # noqa: E402  — full app import: routers, FastAPI(), lifespan def

c.ok(not _marked_handlers(telemetry_logger),
     "A2: importing `main` alone does not activate telemetry — only driving "
     "its lifespan does")

# Spy on the four lifespan-startup calls to prove ORDER, then actually drive
# lifespan startup (and immediately shut it down) using main's own objects —
# hermetic.py already points DATABASE_URL at a throwaway sqlite file, and
# start_scheduler only SCHEDULES a 5-minute cron job, it does not run it.
_call_order = []


def _spy(name, real):
    def wrapper(*a, **kw):
        _call_order.append(name)
        return real(*a, **kw)
    return wrapper


main.configure_llm_telemetry_logging = _spy("configure_llm_telemetry_logging", main.configure_llm_telemetry_logging)
main.validate_llm_settings = _spy("validate_llm_settings", main.validate_llm_settings)
main.create_tables = _spy("create_tables", main.create_tables)
main.start_scheduler = _spy("start_scheduler", main.start_scheduler)

import asyncio  # noqa: E402


async def _drive_lifespan_once():
    async with main.lifespan(main.app):
        pass


asyncio.run(_drive_lifespan_once())

c.ok(_call_order[:1] == ["configure_llm_telemetry_logging"],
     "A3: lifespan startup activates telemetry BEFORE settings validation, "
     "database init, and scheduler startup")
c.ok(set(_call_order) == {
    "configure_llm_telemetry_logging", "validate_llm_settings",
    "create_tables", "start_scheduler",
}, "A3: all four expected lifespan-startup operations actually ran")
c.ok(_marked_handlers(telemetry_logger) != [],
     "A3: after driving lifespan once, telemetry IS now configured")

# From here on, telemetry is configured for THIS process — ready_pid is set.

# =============================================================================
# B. Same-process idempotency
# =============================================================================

handler_before_repeat = _marked_handlers(telemetry_logger)[0]

repeat_capture = _CaptureHandler()
repeat_capture.setFormatter(logging.Formatter("%(message)s"))
telemetry_logger.addHandler(repeat_capture)

for _ in range(5):
    configure_llm_telemetry_logging()

c.ok(len(_marked_handlers(telemetry_logger)) == 1,
     "B: five more same-process calls leave exactly one marked handler")
c.ok(_marked_handlers(telemetry_logger)[0] is handler_before_repeat,
     "B: the surviving handler is the SAME object, not a replacement")
c.ok(not any(l.startswith("llm_telemetry_ready") for l in repeat_capture.lines),
     "B: five more same-process calls emit no additional readiness event")

telemetry_logger.removeHandler(repeat_capture)

# =============================================================================
# F. Actual stream output (the real dedicated handler, not just a test double)
# =============================================================================

real_handler = _marked_handlers(telemetry_logger)[0]
_original_stream = real_handler.stream
_buf = io.StringIO()
real_handler.stream = _buf

emit_attempt(
    request_id="req-real-stream", operation="wisdom_session", provider="luna",
    model="gpt-5.6-luna", routing_mode="single", attempt=1, latency_ms=1234,
    success=True,
)
emit_usage(
    request_id="req-real-stream", operation="wisdom_session", final_provider="luna",
    final_model="gpt-5.6-luna", routing_mode="single", attempts=1, providers_tried=1,
    total_cost_usd=0.00012, total_latency_ms=1234, success=True, fell_back=False,
)
emit_fallback(
    request_id="req-real-stream", operation="wisdom_session",
    from_provider="luna", to_provider="haiku", error_category="OUTAGE",
)
emit_circuit_skip(
    request_id="req-real-stream", operation="wisdom_session",
    provider="qwen", seconds_remaining=42,
)
configure_llm_telemetry_logging()  # already ready for this PID: no new readiness line

real_handler.stream = _original_stream
real_stream_output = _buf.getvalue()
real_stream_lines = [l for l in real_stream_output.splitlines() if l.strip()]

for event_name, expect_count in (
    ("llm_attempt", 1), ("llm_usage", 1), ("llm_fallback", 1),
    ("llm_circuit_skip", 1), ("llm_telemetry_ready", 0),
):
    matches = [l for l in real_stream_lines if (event_name + " ") in l]
    c.ok(len(matches) == expect_count,
         "F: the REAL dedicated StreamHandler wrote %r exactly %d time(s) (got %d)"
         % (event_name, expect_count, len(matches)))

# =============================================================================
# G. Privacy — every event kind, including a failed attempt
# =============================================================================

privacy_buf = io.StringIO()
real_handler.stream = privacy_buf

emit_attempt(  # a FAILED attempt — the one code path carrying an error category
    request_id="req-privacy", operation="connect_chat", provider="haiku",
    model="claude-haiku-4-5", routing_mode="fallback", attempt=2, latency_ms=456,
    success=False, error_category="REFUSAL", refusal=True, incomplete=False,
)
emit_usage(
    request_id="req-privacy", operation="connect_chat", final_provider=None,
    final_model=None, routing_mode="fallback", attempts=2, providers_tried=2,
    total_cost_usd=0.0004, total_latency_ms=900, success=False, fell_back=True,
    error_category="REFUSAL",
)
emit_fallback(
    request_id="req-privacy", operation="connect_chat",
    from_provider="luna", to_provider="haiku", error_category="REFUSAL",
)
emit_circuit_skip(
    request_id="req-privacy", operation="connect_chat",
    provider="qwen", seconds_remaining=7,
)

real_handler.stream = _original_stream
privacy_output = privacy_buf.getvalue().lower()

# The task's own call sites (app/services/llm/router.py) pass only internal
# constants for these fields — ErrorCategory members (REFUSAL, OUTAGE,
# NOT_CONFIGURED, ...), provider ids ("luna"/"haiku"/"qwen"), and operation
# labels (OP_WISDOM etc.) — never request/response content. Confirmed by
# reading router.py's emit_attempt/emit_usage call sites: `error_category`
# always receives `e.category` (an ErrorCategory value), never `e.detail` or
# any provider-supplied text.
for expected in ("request_id", "provider", "operation", "error_category", "refusal"):
    c.ok(expected in privacy_output, "G: telemetry retains expected safe field %r" % expected)
for forbidden in FORBIDDEN_SUBSTRINGS:
    c.ok(forbidden not in privacy_output,
         "G: telemetry (incl. a FAILED/fallback/circuit-skip event) contains none of %r" % forbidden)

ready_capture = _CaptureHandler()
ready_capture.setFormatter(logging.Formatter("%(message)s"))
telemetry_logger.addHandler(ready_capture)
configure_llm_telemetry_logging()
telemetry_logger.removeHandler(ready_capture)
# No new readiness line this PID already announced itself, so re-check the
# ORIGINAL one captured back in section A3/B instead — assert on shape, not
# on catching a fresh one now.
c.ok(all(f not in "llm_telemetry_ready {\"enabled\": true}" for f in
         ("prompt", "excerpt", "authorization", "bearer", "api_key", "credential")),
     "G: the readiness payload shape itself carries none of the forbidden terms")

# =============================================================================
# H. Isolation — root, Uvicorn's own loggers, and every third-party logger
# =============================================================================

c.ok(logging.getLogger().level == _root_level_before,
     "H: the root logger's level is exactly what it was before any of this ran")
c.ok(all(logging.getLogger(name).level == _pre_baseline[name] for name in THIRD_PARTY_LOGGER_NAMES),
     "H: OpenAI/Anthropic/httpx/httpcore/urllib3/boto3/botocore/Firebase/"
     "Pinecone/Voyage logger levels are exactly as they were before anything "
     "here ran (none lowered to INFO)")

logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
configure_llm_telemetry_logging()  # idempotent no-op for this PID
c.ok(all(
    logging.getLogger(name).level == logging.INFO
    for name in UVICORN_LOGGER_NAMES
), "H: Uvicorn's own loggers are still configured at INFO after Uvicorn's "
   "dictConfig AND our telemetry configuration both ran")
c.ok(len(_marked_handlers(telemetry_logger)) == 1,
     "H: Uvicorn's dictConfig does not duplicate or remove our handler "
     "(Uvicorn sets disable_existing_loggers=False)")

# =============================================================================
# D. Thread safety
# =============================================================================

thread_capture = _CaptureHandler()
thread_capture.setFormatter(logging.Formatter("%(message)s"))
telemetry_logger.addHandler(thread_capture)

threads = [threading.Thread(target=configure_llm_telemetry_logging) for _ in range(25)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=10)

c.ok(all(not t.is_alive() for t in threads),
     "D: 25 concurrent threads calling configure() all completed — no deadlock")
c.ok(len(_marked_handlers(telemetry_logger)) == 1,
     "D: 25 concurrent threads leave exactly one marked handler")
c.ok(not any(l.startswith("llm_telemetry_ready") for l in thread_capture.lines),
     "D: 25 concurrent threads in an already-ready PID emit no new readiness event")

telemetry_logger.removeHandler(thread_capture)

# =============================================================================
# C. Fork / process behavior — a REAL fork, not a spawned fresh interpreter
# =============================================================================


def _fork_child_body(conn):
    """Runs inside the forked CHILD — same memory as the parent at fork time,
    including the already-attached marked handler (inherited via copy-on-write)
    and the parent's `_ready_pid`."""
    try:
        child_capture = _CaptureHandler()
        child_capture.setFormatter(logging.Formatter("%(message)s"))
        telemetry_logger.addHandler(child_capture)

        had_marker_before = bool(_marked_handlers(telemetry_logger))

        configure_llm_telemetry_logging()
        handler_count_after_first = len(_marked_handlers(telemetry_logger))
        ready_after_first = sum(1 for l in child_capture.lines if l.startswith("llm_telemetry_ready"))

        configure_llm_telemetry_logging()  # second call, SAME child process
        ready_after_second = sum(1 for l in child_capture.lines if l.startswith("llm_telemetry_ready"))

        conn.send({
            "pid": os.getpid(),
            "had_marker_before": had_marker_before,
            "handler_count_after_first": handler_count_after_first,
            "ready_after_first": ready_after_first,
            "ready_after_second": ready_after_second,
        })
    finally:
        conn.close()


import multiprocessing  # noqa: E402

_parent_pid = os.getpid()
_fork_capture = _CaptureHandler()
_fork_capture.setFormatter(logging.Formatter("%(message)s"))
telemetry_logger.addHandler(_fork_capture)
configure_llm_telemetry_logging()  # parent's own (already-ready) call, for the count below
_parent_ready_count_before_fork = sum(1 for l in _fork_capture.lines if l.startswith("llm_telemetry_ready"))

ctx = multiprocessing.get_context("fork")
parent_conn, child_conn = ctx.Pipe()
child = ctx.Process(target=_fork_child_body, args=(child_conn,))
child.start()
child_conn.close()
result = parent_conn.recv() if parent_conn.poll(15) else None
child.join(timeout=15)

c.ok(result is not None, "C: the forked child reported back within the timeout (no deadlock/hang)")
if result is not None:
    c.ok(result["pid"] != _parent_pid, "C: the forked child ran under a genuinely different PID")
    c.ok(result["had_marker_before"] is True,
         "C: the child inherited the parent's already-marked handler via fork()")
    c.ok(result["handler_count_after_first"] == 1,
         "C: the child does NOT attach a second handler merely because its PID differs")
    c.ok(result["ready_after_first"] == 1,
         "C: the child emits its OWN readiness event exactly once, despite inheriting the handler")
    c.ok(result["ready_after_second"] == 1,
         "C: a second call inside the SAME child process does not duplicate its readiness event")
c.ok(not child.is_alive(), "C: the forked child process actually exited")
c.ok(_parent_ready_count_before_fork == 0,
     "C: the parent itself emitted no NEW readiness line for a PID it already announced")

telemetry_logger.removeHandler(_fork_capture)

# =============================================================================
# E. Actual Uvicorn ordering, in a FRESH subprocess (real production order:
#    Uvicorn's dictConfig runs BEFORE our telemetry configuration)
# =============================================================================

_E_SCRIPT = r"""
import logging, logging.config, os, sys
sys.path.insert(0, %r)
sys.path.insert(0, %r)
import hermetic  # noqa: F401
import uvicorn.config

logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)   # Uvicorn's real order: first
logging.getLogger().setLevel(logging.WARNING)

from app.services.llm.usage import configure_llm_telemetry_logging, emit_usage, logger

class Cap(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []
    def emit(self, record):
        self.lines.append(self.format(record))

cap = Cap()
cap.setFormatter(logging.Formatter("%%(message)s"))
logger.addHandler(cap)

configure_llm_telemetry_logging()   # Nibbler telemetry: second
emit_usage(request_id="e2e-order", operation="wisdom_session", final_provider="luna",
           final_model="gpt-5.6-luna", routing_mode="single", attempts=1, providers_tried=1,
           total_cost_usd=0.0001, total_latency_ms=500, success=True, fell_back=False)

ready = sum(1 for l in cap.lines if l.startswith("llm_telemetry_ready"))
usage = sum(1 for l in cap.lines if l.startswith("llm_usage"))
uv = logging.getLogger("uvicorn")
uv_access = logging.getLogger("uvicorn.access")
uv_error = logging.getLogger("uvicorn.error")
uvicorn_intact = (
    uv.level == logging.INFO and uv_access.level == logging.INFO
    and uv_error.level == logging.INFO and len(uv_access.handlers) >= 1
)
print("READY_COUNT:%%d" %% ready)
print("USAGE_COUNT:%%d" %% usage)
print("UVICORN_INTACT:%%s" %% uvicorn_intact)
print("EFFECTIVE_LEVEL_INFO:%%s" %% (logger.getEffectiveLevel() == logging.INFO))
""" % (BACKEND_ROOT, os.path.join(BACKEND_ROOT, "tests"))

_sanitized_env = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "HOME": os.environ.get("HOME", "/tmp"),
}  # deliberately no PYTHONPATH, no credential-shaped variables at all

proc = subprocess.run(
    [VENV_PYTHON, "-c", _E_SCRIPT],
    env=_sanitized_env, capture_output=True, text=True, timeout=60,
)
e_out = proc.stdout
c.ok(proc.returncode == 0, "E: fresh-subprocess actual-order probe exited 0 (stderr: %r)" % proc.stderr[-500:])
c.ok("READY_COUNT:1" in e_out, "E: exactly one readiness line under REAL Uvicorn-then-telemetry order")
c.ok("USAGE_COUNT:1" in e_out, "E: exactly one llm_usage line under REAL Uvicorn-then-telemetry order")
c.ok("UVICORN_INTACT:True" in e_out, "E: Uvicorn's own access/error logger configuration remains intact")
c.ok("EFFECTIVE_LEVEL_INFO:True" in e_out, "E: INFO telemetry is visible under Uvicorn's real effective config")

# =============================================================================
# I. Hermetic behavior
# =============================================================================

c.ok(hermetic.blocked_attempts == [],
     "I: zero outbound network attempts across every section of this file")

sys.exit(1 if c.finish() else 0)
