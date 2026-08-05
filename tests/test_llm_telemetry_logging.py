"""
Regression tests for the production LLM-telemetry visibility fix.

Gate 5 generated a real, successful production Luna session and Railway's
logs contained no `llm_attempt`/`llm_usage` line: nothing in the app ever
configures the ROOT logger (no `logging.basicConfig`), so the telemetry
module's `logger.info(...)` calls inherited the default root level, WARNING,
and were silently dropped — while Uvicorn's own access/startup lines still
appeared, because Uvicorn configures its OWN three loggers explicitly.

These tests prove the fix in `app/services/llm/usage.configure_llm_telemetry_logging`
is scoped to exactly the one logger it needs to be, is idempotent, and leaves
Uvicorn's and every third-party library's logging exactly as it found them.

    .venv/bin/python tests/test_llm_telemetry_logging.py
"""

import logging
import logging.config
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermetic  # noqa: E402,F401

from llm_fakes import Checks  # noqa: E402

import uvicorn.config  # noqa: E402

from app.services.llm.usage import (  # noqa: E402
    _TELEMETRY_HANDLER_MARKER,
    configure_llm_telemetry_logging,
    emit_attempt,
    emit_usage,
    logger as telemetry_logger,
)

c = Checks("LLM telemetry production-logging fix")

THIRD_PARTY_LOGGER_NAMES = (
    "openai", "anthropic", "httpx", "httpcore", "urllib3", "boto3", "botocore",
    "firebase_admin", "pinecone", "voyageai",
)

FORBIDDEN_SUBSTRINGS = (
    "prompt", "excerpt", "chat history", "card body", "authorization",
    "api_key", "api key", "bearer",
)


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))


def _has_marked_handler(lg):
    return [h for h in lg.handlers if getattr(h, _TELEMETRY_HANDLER_MARKER, False)]


# ── 1. Importing the module alone must not mutate global logging ───────────
# Must run FIRST and before any call to configure_llm_telemetry_logging()
# anywhere below — once called, the marker is permanent for this process.
c.ok(not _has_marked_handler(telemetry_logger),
     "importing app.services.llm.usage alone attaches no telemetry handler")
c.ok(telemetry_logger.level != logging.INFO,
     "importing alone does not raise the logger's own level to INFO")

# Baseline third-party logger levels, captured BEFORE any configuration call,
# so a later comparison actually proves nothing touched them.
_baseline_levels = {name: logging.getLogger(name).level for name in THIRD_PARTY_LOGGER_NAMES}

# ── 2. Simulate the real production condition: unconfigured root at WARNING ─
logging.getLogger().setLevel(logging.WARNING)
c.ok(telemetry_logger.getEffectiveLevel() != logging.INFO,
     "before the fix, the telemetry logger's effective level is NOT INFO "
     "(this is the bug Gate 5 hit)")

# ── 3. First call: attaches one handler, sets INFO, emits readiness once ───
# Both capture handlers are attached BEFORE the first call, so they are
# present to actually witness the one-time readiness event when it fires.
root_capture = _CaptureHandler()
root_capture.setFormatter(logging.Formatter("%(message)s"))
logging.getLogger().addHandler(root_capture)

test_capture = _CaptureHandler()
test_capture.setFormatter(logging.Formatter("%(message)s"))
telemetry_logger.addHandler(test_capture)

returned = configure_llm_telemetry_logging()
c.ok(returned is telemetry_logger, "configure_llm_telemetry_logging returns the telemetry logger")
c.ok(telemetry_logger.getEffectiveLevel() == logging.INFO,
     "after configuration, INFO telemetry is visible even though root is WARNING")

marked = _has_marked_handler(telemetry_logger)
c.ok(len(marked) == 1, "exactly one marked handler is attached after the first call")
c.ok(telemetry_logger.propagate is False,
     "propagation is disabled so root handlers cannot double-print the same event")
c.ok(len(root_capture.lines) == 0,
     "the readiness event does NOT also reach the root logger (propagate=False works)")

ready_lines = [l for l in test_capture.lines if l.startswith("llm_telemetry_ready ")]
c.ok(len(ready_lines) == 1, "the startup readiness event was emitted exactly once")
c.ok('"enabled": true' in ready_lines[0] if ready_lines else False,
     "the readiness event says enabled:true and carries nothing else")
c.ok(not any(env_word in ready_lines[0].lower() for env_word in ("key", "://", "@"))
     if ready_lines else False,
     "the readiness event contains no environment/credential-shaped content")

# ── 4. Repeated configuration: idempotent, no duplicate handler or event ───
test_capture.lines.clear()
configure_llm_telemetry_logging()
configure_llm_telemetry_logging()
marked_after_repeat = _has_marked_handler(telemetry_logger)
c.ok(len(marked_after_repeat) == 1, "calling configure twice more still leaves exactly one handler")
c.ok(marked_after_repeat[0] is marked[0], "the surviving handler is the SAME object, not a replacement")
c.ok(len([l for l in test_capture.lines if l.startswith("llm_telemetry_ready")]) == 0,
     "repeated configuration does not emit a second readiness event")

# ── 5. Behaves the same under Uvicorn's REAL effective logging config ──────
logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
configure_llm_telemetry_logging()  # idempotent no-op; telemetry must still work
c.ok(telemetry_logger.getEffectiveLevel() == logging.INFO,
     "INFO telemetry is still visible after Uvicorn applies its own dictConfig")
marked_after_uvicorn = _has_marked_handler(telemetry_logger)
c.ok(len(marked_after_uvicorn) == 1,
     "Uvicorn's dictConfig does not duplicate or remove our handler "
     "(uvicorn sets disable_existing_loggers=False)")

uv = logging.getLogger("uvicorn")
uv_access = logging.getLogger("uvicorn.access")
uv_error = logging.getLogger("uvicorn.error")
c.ok(uv.level == logging.INFO and uv.propagate is False and len(uv.handlers) >= 1,
     "Uvicorn's own 'uvicorn' logger remains configured as Uvicorn set it")
c.ok(uv_access.level == logging.INFO and uv_access.propagate is False and len(uv_access.handlers) >= 1,
     "Uvicorn's own 'uvicorn.access' logger remains configured as Uvicorn set it")
c.ok(uv_error.level == logging.INFO,
     "Uvicorn's own 'uvicorn.error' logger remains configured as Uvicorn set it")

# ── 6. Third-party SDK/HTTP loggers were never touched ──────────────────────
c.ok(
    all(logging.getLogger(name).level == _baseline_levels[name] for name in THIRD_PARTY_LOGGER_NAMES),
    "OpenAI/Anthropic/httpx/urllib3/boto3/Firebase/Pinecone/Voyage logger "
    "levels are exactly as they were before configuration (none lowered to INFO)",
)

# ── 7. Real events: emitted exactly once each, with safe content only ──────
test_capture.lines.clear()
emit_attempt(
    request_id="req-test-0001", operation="wisdom_session", provider="luna",
    model="gpt-5.6-luna", routing_mode="single", attempt=1, latency_ms=1234,
    success=True,
)
attempt_lines = [l for l in test_capture.lines if " llm_attempt " in l or l.startswith("llm_attempt ")]
c.ok(len(attempt_lines) == 1, "llm_attempt is emitted exactly once per call")

test_capture.lines.clear()
emit_usage(
    request_id="req-test-0001", operation="wisdom_session", final_provider="luna",
    final_model="gpt-5.6-luna", routing_mode="single", attempts=1, providers_tried=1,
    total_cost_usd=0.00012, total_latency_ms=1234, success=True, fell_back=False,
)
usage_lines = [l for l in test_capture.lines if " llm_usage " in l or l.startswith("llm_usage ")]
c.ok(len(usage_lines) == 1, "llm_usage is emitted exactly once per call")

combined = "\n".join(attempt_lines + usage_lines).lower()
for expected in ("request_id", "provider", "model", "routing_mode", "latency_ms", "success"):
    c.ok(expected in combined, "telemetry line contains expected safe field %r" % expected)
for forbidden in FORBIDDEN_SUBSTRINGS:
    c.ok(forbidden not in combined, "telemetry line contains none of the forbidden terms: %r" % forbidden)

logging.getLogger().removeHandler(root_capture)
telemetry_logger.removeHandler(test_capture)

# ── 8. No network call was made anywhere in this file ──────────────────────
c.ok(hermetic.blocked_attempts == [], "configuring telemetry made zero outbound network calls")

sys.exit(1 if c.finish() else 0)
