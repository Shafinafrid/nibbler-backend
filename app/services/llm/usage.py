"""
Normalized token usage, cost estimation, and the three telemetry events.

Three providers report usage in three shapes. This module flattens them into
one `Usage` record so a cost question ("what did yesterday's nibbles cost, and
would Haiku have been cheaper?") can be answered without knowing which SDK
produced which number.

The single most important field is `reasoning_tokens`. Luna's reasoning is
hidden but BILLED AS OUTPUT, so a naive "output tokens × output price" using
only the visible text understates the bill — sometimes badly. `output_tokens`
here is therefore always the *total* generated count, with `visible_output_tokens`
tracked alongside it for prompt-sizing work.

Nothing in this module may log prompt text, excerpts, chat history or generated
content. The events carry counts, categories and identifiers — never payloads.
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


# ── operation labels ────────────────────────────────────────────────────────
# Stable strings: they end up in log queries and cost rollups, so renaming one
# silently breaks history.
OP_ASPIRATION = "aspiration_interpretation"
OP_WISDOM = "wisdom_session"
OP_STORY = "story_metadata"
OP_CONNECT = "connect_chat"


class Usage:
    """Token counts for one provider attempt, in one shape.

    All fields default to 0 rather than None: a provider that omits a count is
    reporting "nothing here", and 0 keeps the cost arithmetic total. Where a
    provider genuinely cannot report (a local server without usage), the caller
    leaves the counts at 0 and the estimated cost is legitimately 0.
    """

    __slots__ = (
        "input_tokens", "cached_input_tokens", "cache_write_tokens",
        "output_tokens", "visible_output_tokens", "reasoning_tokens",
    )

    def __init__(
        self,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        cache_write_tokens: int = 0,
        output_tokens: int = 0,
        visible_output_tokens: int = 0,
        reasoning_tokens: int = 0,
    ):
        self.input_tokens = int(input_tokens or 0)
        self.cached_input_tokens = int(cached_input_tokens or 0)
        self.cache_write_tokens = int(cache_write_tokens or 0)
        self.output_tokens = int(output_tokens or 0)
        self.visible_output_tokens = int(visible_output_tokens or 0)
        self.reasoning_tokens = int(reasoning_tokens or 0)

    def as_dict(self) -> Dict[str, int]:
        return {k: getattr(self, k) for k in self.__slots__}

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return "Usage(%s)" % self.as_dict()


class Price:
    """USD per 1M tokens for one model.

    EDITABLE AUDIT ESTIMATE. These are copied from published price pages and
    are not authoritative — the provider's invoice is. They exist so relative
    cost between providers is visible in logs without a billing-console trip.
    """

    __slots__ = ("input", "cached_input", "cache_write", "output")

    def __init__(self, input: float, cached_input: float, cache_write: float, output: float):
        self.input = input
        self.cached_input = cached_input
        self.cache_write = cache_write
        self.output = output


# ── pricing table (USD per 1M tokens) — EDITABLE AUDIT ESTIMATES ────────────
# Verified 2026-08-02 against the providers' published pricing.
PRICING: Dict[str, Price] = {
    # OpenAI GPT-5.6 Luna. Hidden reasoning tokens bill at the output rate,
    # which is why cost uses total output rather than visible output.
    "gpt-5.6-luna": Price(input=0.20, cached_input=0.02, cache_write=0.25, output=1.20),
    # Anthropic Claude Haiku 4.5. Cache writes bill at 1.25x input, cache
    # reads at 0.1x — the standard Anthropic ephemeral-cache multipliers.
    "claude-haiku-4-5": Price(input=1.00, cached_input=0.10, cache_write=1.25, output=5.00),
}

# Luna bills long prompts at a premium. Nibbler's prompts are two orders of
# magnitude below this, but a future long-context feature would silently make
# every logged cost wrong, so the surcharge is modelled rather than assumed away.
LUNA_LONG_PROMPT_THRESHOLD = 272_000
LUNA_LONG_INPUT_MULTIPLIER = 2.0
LUNA_LONG_OUTPUT_MULTIPLIER = 1.5


def estimate_cost(model: str, usage: Usage) -> float:
    """Estimated USD for one attempt. Returns 0.0 for models with no price.

    Self-hosted Qwen has no per-token API price, so it lands here as 0.0. That
    is an API cost of zero, NOT a cost of zero: the Mac, its electricity and
    the tunnel are real and unmeasured. Anywhere this number is reported it
    must be described as API spend.
    """
    price = PRICING.get(model)
    if price is None:
        return 0.0

    uncached_input = max(0, usage.input_tokens - usage.cached_input_tokens)
    in_rate, out_rate = price.input, price.output
    if model.startswith("gpt-5.6") and usage.input_tokens > LUNA_LONG_PROMPT_THRESHOLD:
        in_rate *= LUNA_LONG_INPUT_MULTIPLIER
        out_rate *= LUNA_LONG_OUTPUT_MULTIPLIER

    total = (
        uncached_input * in_rate
        + usage.cached_input_tokens * price.cached_input
        + usage.cache_write_tokens * price.cache_write
        + usage.output_tokens * out_rate
    ) / 1_000_000.0
    return round(total, 8)


# ── telemetry ───────────────────────────────────────────────────────────────
# One log line per event, payload as JSON so it can be grepped and parsed out
# of Railway's plain-text logs. Follows the repo's stdlib-logging convention.

def _emit(event: str, payload: Dict[str, Any]) -> None:
    try:
        logger.info("%s %s", event, json.dumps(payload, sort_keys=True, default=str))
    except Exception:  # pragma: no cover — telemetry must never break a request
        logger.info("%s <unserializable>", event)


def emit_attempt(
    *,
    request_id: str,
    operation: str,
    provider: str,
    model: str,
    routing_mode: str,
    attempt: int,
    latency_ms: int,
    success: bool,
    usage: Optional[Usage] = None,
    reasoning: Optional[str] = None,
    error_category: Optional[str] = None,
    refusal: bool = False,
    incomplete: bool = False,
) -> None:
    """One provider call — succeeded or not. Emitted for EVERY attempt.

    A failed attempt still costs money at most providers, so it is recorded
    with its usage rather than dropped; `success` is what separates the two.

    `latency_ms` is THIS attempt's duration, not elapsed time since the request
    began — otherwise the second provider in a chain inherits the first one's
    timeout and every fallback looks slow.

    `request_id` ties the attempts, the fallback and the summary of one logical
    request together. Without it, concurrent requests interleave in the log and
    "which attempts belonged to the deck that failed?" is unanswerable.
    """
    payload: Dict[str, Any] = {
        "request_id": request_id,
        "operation": operation,
        "provider": provider,
        "model": model,
        "routing_mode": routing_mode,
        "attempt": attempt,
        "latency_ms": latency_ms,
        "success": success,
        "refusal": refusal,
        "incomplete": incomplete,
    }
    if reasoning:
        payload["reasoning"] = reasoning
    if error_category:
        payload["error_category"] = error_category
    if usage is not None:
        payload.update(usage.as_dict())
        payload["estimated_cost_usd"] = estimate_cost(model, usage)
    _emit("llm_attempt", payload)


def emit_usage(
    *,
    request_id: str,
    operation: str,
    final_provider: Optional[str],
    final_model: Optional[str],
    routing_mode: str,
    attempts: int,
    providers_tried: int,
    total_cost_usd: float,
    total_latency_ms: int,
    success: bool,
    fell_back: bool,
    error_category: Optional[str] = None,
) -> None:
    """The whole logical request, once.

    `total_cost_usd` INCLUDES failed attempts and retries — a fallback that
    cost two calls should read as two calls, otherwise the cheap-provider-first
    strategy looks free when it is not.

    `fell_back` means a DIFFERENT provider answered, which is why it is derived
    from `providers_tried` and not from the attempt count: a same-provider
    retry is two attempts at one provider and is not a fallback. Conflating
    them makes retries look like provider failures in the rollup.
    """
    _emit("llm_usage", {
        "request_id": request_id,
        "operation": operation,
        "providers_tried": providers_tried,
        "final_provider": final_provider,
        "final_model": final_model,
        "routing_mode": routing_mode,
        "attempts": attempts,
        "total_cost_usd": round(total_cost_usd, 8),
        "total_latency_ms": total_latency_ms,
        "success": success,
        "fell_back": fell_back,
        "error_category": error_category,
    })


def emit_fallback(
    *,
    request_id: str,
    operation: str,
    from_provider: str,
    to_provider: str,
    error_category: str,
) -> None:
    """A provider handed off to the next one. Rare in a healthy system, so
    these lines are the ones worth alerting on."""
    _emit("llm_fallback", {
        "request_id": request_id,
        "operation": operation,
        "from_provider": from_provider,
        "to_provider": to_provider,
        "error_category": error_category,
    })


def emit_circuit_skip(*, request_id: str, operation: str, provider: str,
                      seconds_remaining: int) -> None:
    """A provider was skipped without being called, because its breaker is open."""
    _emit("llm_circuit_skip", {
        "request_id": request_id,
        "operation": operation,
        "provider": provider,
        "seconds_remaining": seconds_remaining,
    })


# ── making the telemetry above actually visible in production ──────────────
# `logger` above is this module's own `getLogger(__name__)` — every event this
# file emits already goes through it. Nothing in the app ever configures the
# ROOT logger (no `logging.basicConfig`), so a bare `logger.info(...)` here
# inherits Python's default root level, WARNING, and is silently dropped. That
# is why Railway showed uvicorn's own access/startup lines (uvicorn configures
# "uvicorn"/"uvicorn.error"/"uvicorn.access" itself, explicitly, at INFO) but
# never an `llm_attempt` or `llm_usage` line, even for a real, successful call.
#
# The fix is scoped to exactly this one logger — not the root logger, not any
# third-party SDK logger — so it cannot turn on verbose OpenAI/Anthropic/httpx/
# urllib3/boto3/Firebase/Pinecone/Voyage output as a side effect.
_TELEMETRY_HANDLER_MARKER = "_nibbler_llm_telemetry_handler"


def configure_llm_telemetry_logging() -> logging.Logger:
    """Make this module's INFO-level telemetry visible, exactly once per process.

    Idempotent by construction: the marker attribute on the handler (not a
    handler *count*) is what gets checked, so calling this twice — a stray
    duplicate import, calling it from more than one startup path — attaches
    nothing a second time and does not double every future log line. Uvicorn
    sets `disable_existing_loggers: False` in its own dictConfig (verified
    against the installed version), so this logger's level/handler survive
    regardless of whether uvicorn's own logging setup runs before or after
    this call.

    Safe to call before uvicorn's app import completes: it touches only the
    logger named after this module, never the root logger and never uvicorn's
    own "uvicorn"/"uvicorn.access"/"uvicorn.error" loggers.
    """
    logger.setLevel(logging.INFO)

    if not any(getattr(h, _TELEMETRY_HANDLER_MARKER, False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"
        ))
        setattr(handler, _TELEMETRY_HANDLER_MARKER, True)
        logger.addHandler(handler)
        # This logger now has its own handler; letting the record ALSO
        # propagate to the root logger's handlers (present or future) would
        # print every event twice.
        logger.propagate = False
        # One harmless line, carrying no environment, provider, model or
        # account information — proves telemetry is live without needing a
        # real (paid) provider call to verify it in production.
        _emit("llm_telemetry_ready", {"enabled": True})

    return logger
