"""
Which provider gets called, how many times, and when the chain stops.

This is the only module that decides those things. Adapters know how to talk to
one provider; workflows know what to ask for; the router owns the policy in
between, so changing the fallback order is a config edit and never a code edit.

The two modes are not variations of each other:

  * **single** calls exactly one provider. Not "prefers" — a silent fallback
    would mean judging Luna's output quality while actually reading Haiku's,
    which defeats the only reason the mode exists. A bounded same-provider
    retry is still allowed; crossing to another provider is not.
  * **fallback** walks the configured order and stops at the first response
    that passes both schema parsing and semantic validation.

Boundedness is the safety property. Per logical request, each provider is
attempted at most twice — once, plus one retry reserved for output that came
back malformed — and each provider appears at most once in the chain. There is
no loop that can revisit a provider, so a bad day cannot multiply the bill.

What stops the chain early:

  * a **safety refusal** — trying the next model until one complies is exactly
    the behaviour that must not exist;
  * a **bad request**, which is our bug and would fail identically everywhere;
  * anything not in `ELIGIBLE_FOR_FALLBACK`.
"""

import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from .base import LLMRequest, LLMResult
from .circuit import CircuitBreaker
from .errors import ErrorCategory, ProviderError
from .haiku import ALLOWED_HAIKU_MODELS, HaikuAdapter
from .luna import ALLOWED_LUNA_MODELS, ALLOWED_REASONING_EFFORTS, LunaAdapter
from .qwen import QwenAdapter, is_local_url
from .usage import emit_attempt, emit_circuit_skip, emit_fallback, emit_usage, estimate_cost

logger = logging.getLogger(__name__)

PROVIDER_IDS = ("luna", "haiku", "qwen")
ROUTING_MODES = ("single", "fallback")

# The smallest llama-server context window that can serve Nibbler's largest
# request, DERIVED from the constants rather than guessed — a hand-picked
# number drifts the moment a card target or a chunk size changes.
#
# Worst case is a 15-minute Wisdom deck:
_WISDOM_SYSTEM_TOKENS = 700        # SESSION_SYSTEM, measured
_WISDOM_CHUNKS = 14                # WISDOM_TOP_K[15]
_CHUNK_TOKENS = 500                # embedding_service chunk size
_WISDOM_PROFILE_TOKENS = 300       # profile, targets, interaction instruction
_WISDOM_OUTPUT_TOKENS = 8000       # WISDOM_MAX_TOKENS_CEILING

_QWEN_REQUIRED_TOKENS = (
    _WISDOM_SYSTEM_TOKENS
    + _WISDOM_CHUNKS * _CHUNK_TOKENS
    + _WISDOM_PROFILE_TOKENS
    + _WISDOM_OUTPUT_TOKENS
)  # = 16,000

# A window sized to the estimate exactly leaves no room for the estimate being
# wrong, and being wrong here does not raise an error — llama-server silently
# drops the START of an over-long prompt, which is where the instructions live.
# So: 1.5x, rounded up to a power of two. The audit was right that 16,384 sat
# BELOW the real requirement.
QWEN_SAFETY_MARGIN = 1.5
QWEN_MIN_CONTEXT_TOKENS = 24576

assert QWEN_MIN_CONTEXT_TOKENS >= _QWEN_REQUIRED_TOKENS * QWEN_SAFETY_MARGIN * 0.99, (
    "QWEN_MIN_CONTEXT_TOKENS no longer covers the derived requirement"
)

# One retry, and only for output that came back wrong — not for a 500, which a
# second identical call would just collect again.
MAX_ATTEMPTS_PER_PROVIDER = 2


class LLMConfigError(Exception):
    """Routing configuration is unusable. Raised at startup, never per request."""


def parse_fallback_order(raw: str) -> List[str]:
    return [p.strip().lower() for p in (raw or "").split(",") if p.strip()]


def validate_llm_settings(settings) -> List[str]:
    """Check the routing configuration. Returns warnings; raises on errors.

    Runs at startup so a misconfiguration is a boot failure with a readable
    message, rather than a 502 the first time a user opens a nibble.

    **Makes no network calls and no paid calls.** A provider is "configured"
    when it has the settings it needs, which is knowable without asking it.
    """
    errors: List[str] = []
    warnings: List[str] = []

    mode = (settings.llm_routing_mode or "").strip().lower()
    if mode not in ROUTING_MODES:
        errors.append("LLM_ROUTING_MODE=%r is not one of %s" % (mode, list(ROUTING_MODES)))

    enabled = {
        "luna": bool(settings.llm_luna_enabled),
        "haiku": bool(settings.llm_haiku_enabled),
        "qwen": bool(settings.llm_qwen_enabled),
    }

    # Reasoning is validated whenever Luna could be reached at all — a cap that
    # only applies in some modes is not a cap.
    effort = (settings.openai_llm_reasoning_effort or "").strip().lower()
    if effort not in ALLOWED_REASONING_EFFORTS:
        errors.append(
            "OPENAI_LLM_REASONING_EFFORT=%r exceeds the 'low' cap (allowed: %s)"
            % (effort, list(ALLOWED_REASONING_EFFORTS))
        )
    # Exact match, not a prefix. `startswith` accepted `gpt-5.6-luna-preview`
    # or any future suffix, which is a different model with different pricing
    # and different behaviour — precisely the substitution this check exists to
    # prevent. Dated snapshots are listed explicitly when one is adopted.
    model = (settings.openai_llm_model or "").strip()
    if enabled["luna"] and model not in ALLOWED_LUNA_MODELS:
        errors.append(
            "OPENAI_LLM_MODEL=%r is not an approved Luna model (allowed: %s). "
            "The bare 'gpt-5.6' alias routes to Sol." % (model, sorted(ALLOWED_LUNA_MODELS))
        )

    # Same reasoning for Haiku: a substring test for "haiku" happily accepted
    # claude-haiku-3, an older and materially weaker model.
    haiku_model = (settings.anthropic_llm_model or "").strip()
    if enabled["haiku"] and haiku_model not in ALLOWED_HAIKU_MODELS:
        errors.append(
            "ANTHROPIC_LLM_MODEL=%r is not an approved Haiku 4.5 model (allowed: %s)"
            % (haiku_model, sorted(ALLOWED_HAIKU_MODELS))
        )

    # Which providers must actually be usable depends on the mode: a Luna-only
    # deployment has no business demanding an Anthropic key.
    active = (settings.llm_active_provider or "").strip().lower()
    if mode == "single":
        if active not in PROVIDER_IDS:
            errors.append("LLM_ACTIVE_PROVIDER=%r is not one of %s" % (active, list(PROVIDER_IDS)))
        elif not enabled[active]:
            errors.append("LLM_ACTIVE_PROVIDER=%r is disabled by LLM_%s_ENABLED"
                          % (active, active.upper()))
        required = [active] if active in PROVIDER_IDS else []
    else:
        order = parse_fallback_order(settings.llm_fallback_order)
        if not order:
            errors.append("LLM_FALLBACK_ORDER is empty")
        unknown = [p for p in order if p not in PROVIDER_IDS]
        if unknown:
            errors.append("LLM_FALLBACK_ORDER contains unknown provider(s): %s" % unknown)
        if len(set(order)) != len(order):
            errors.append("LLM_FALLBACK_ORDER contains duplicates: %s" % order)
        disabled = [p for p in order if p in PROVIDER_IDS and not enabled[p]]
        if disabled:
            errors.append("LLM_FALLBACK_ORDER lists disabled provider(s): %s" % disabled)
        required = [p for p in order if p in PROVIDER_IDS and enabled.get(p)]
        if not required and not errors:
            errors.append("no usable provider: every entry in LLM_FALLBACK_ORDER is disabled")

    for provider in required:
        adapter = build_adapter(provider, settings)
        if adapter is not None and not adapter.is_configured():
            errors.append(
                "provider %r is in the active routing configuration but is missing "
                "required settings (see .env.example)" % provider
            )

    if "qwen" in required and settings.qwen_base_url:
        url = settings.qwen_base_url
        local = is_local_url(url)
        is_production = (getattr(settings, "app_env", "") or "").strip().lower() == "production"
        if local and is_production:
            # In production this is not a warning to read later — it is a
            # provider that can never answer. `127.0.0.1` inside a Railway
            # container is the container, so every Qwen call fails at connect
            # time, and the only signal is a log line nobody is watching.
            errors.append(
                "QWEN_BASE_URL=%r is a loopback/private address and APP_ENV=production — "
                "a Railway container cannot reach the MacBook that way. Use the HTTPS "
                "tunnel URL, or disable Qwen with LLM_QWEN_ENABLED=false." % url
            )
        elif local:
            # Outside production this is the normal local-dev setup.
            warnings.append(
                "QWEN_BASE_URL points at a loopback/private address — fine for local "
                "development, but a Railway container cannot reach the MacBook that way"
            )
        elif not url.lower().startswith("https://"):
            # A public plaintext endpoint is an error, not a warning: every
            # request would put a user's book excerpts and the bearer token on
            # the wire in clear text. (Plain HTTP to your own machine is fine.)
            errors.append(
                "QWEN_BASE_URL=%r is a public endpoint over plain HTTP — book excerpts "
                "and the API key would travel unencrypted. Use https://." % url
            )

        ctx = int(getattr(settings, "qwen_context_size", 0) or 0)
        if ctx < QWEN_MIN_CONTEXT_TOKENS:
            errors.append(
                "QWEN_CONTEXT_SIZE=%d is too small — a 15-minute Wisdom deck needs "
                "~8.7K input plus 8K output, and llama-server silently truncates the "
                "START of an over-long prompt, which is where the instructions live. "
                "Use at least %d." % (ctx, QWEN_MIN_CONTEXT_TOKENS)
            )

    if errors:
        raise LLMConfigError("Invalid LLM routing configuration:\n  - " + "\n  - ".join(errors))
    return warnings


# ⚠️ ONE breaker for the whole process, deliberately module-level.
#
# `LLMService()` is constructed per request — in the Connect router, the
# profile router and the session service. A breaker owned by the router
# instance therefore died with the request that opened it, which made the
# feature decorative: every request re-discovered the same 401 or the same
# sleeping MacBook at full price. The state has to outlive the request to mean
# anything, and the object that outlives the request is the module.
#
# Still process-local (see circuit.py): it does not coordinate across replicas
# and does not survive a restart.
_SHARED_BREAKER: Optional[CircuitBreaker] = None


def get_shared_breaker(cooldown_seconds: int = 120) -> CircuitBreaker:
    global _SHARED_BREAKER
    if _SHARED_BREAKER is None:
        _SHARED_BREAKER = CircuitBreaker(cooldown_seconds)
    return _SHARED_BREAKER


def reset_shared_breaker() -> None:
    """Drop the process-wide breaker. For tests — nothing in the app calls it."""
    global _SHARED_BREAKER
    _SHARED_BREAKER = None


def build_adapter(provider: str, settings, client=None):
    if provider == "luna":
        return LunaAdapter(settings, client=client)
    if provider == "haiku":
        return HaikuAdapter(settings, client=client)
    if provider == "qwen":
        return QwenAdapter(settings, client=client)
    return None


class LLMRouter:
    """Executes one logical request across the configured provider chain."""

    def __init__(self, settings, adapters: Optional[Dict[str, Any]] = None,
                 breaker: Optional[CircuitBreaker] = None):
        self._settings = settings
        self.mode = (settings.llm_routing_mode or "fallback").strip().lower()
        # Shared across every LLMService in the process — a per-request breaker
        # forgets everything it learned before the next request starts.
        self.breaker = breaker if breaker is not None else get_shared_breaker(
            getattr(settings, "llm_circuit_cooldown_seconds", 120)
        )
        self._adapters: Dict[str, Any] = dict(adapters or {})
        self._enabled = {
            "luna": bool(settings.llm_luna_enabled),
            "haiku": bool(settings.llm_haiku_enabled),
            "qwen": bool(settings.llm_qwen_enabled),
        }

    # ── chain construction ───────────────────────────────────────────────

    def provider_chain(self) -> List[str]:
        """The providers this request may use, in order.

        In single mode this is one element by construction, which is the
        mechanism that makes "never silently falls back" true rather than
        merely intended — there is no second provider in the list to reach.
        """
        if self.mode == "single":
            active = (self._settings.llm_active_provider or "").strip().lower()
            return [active] if self._enabled.get(active) else []
        seen = set()
        chain = []
        for p in parse_fallback_order(self._settings.llm_fallback_order):
            if p in PROVIDER_IDS and self._enabled.get(p) and p not in seen:
                seen.add(p)
                chain.append(p)
        return chain

    def _adapter(self, provider: str):
        if provider not in self._adapters:
            self._adapters[provider] = build_adapter(provider, self._settings)
        return self._adapters[provider]

    # ── execution ────────────────────────────────────────────────────────

    def run(
        self,
        request: LLMRequest,
        finalize: Optional[Callable[[LLMResult], Any]] = None,
    ) -> LLMResult:
        """Run `request` down the chain and return the first good result.

        `finalize` is the semantic validator. It runs INSIDE the attempt loop
        on purpose: a deck that parses but breaks a product rule is a failed
        attempt, eligible for the same retry-then-fall-back treatment as
        malformed JSON. Validating after the router returned would leave the
        only options "accept it" or "fail the whole request".

        Raises the last `ProviderError` when nothing succeeds.
        """
        chain = self.provider_chain()
        if not chain:
            raise ProviderError(
                ErrorCategory.NOT_CONFIGURED, "router",
                "no usable provider for routing mode %r" % self.mode,
            )

        # One id for the whole logical request. Without it, concurrent requests
        # interleave in the log and there is no way to ask which attempts
        # belonged to the deck that failed.
        request_id = uuid.uuid4().hex[:12]
        started = time.time()
        total_cost = 0.0
        attempts = 0
        providers_tried = 0
        last_error: Optional[ProviderError] = None

        for index, provider in enumerate(chain):
            adapter = self._adapter(provider)
            if adapter is None or not adapter.is_configured():
                last_error = ProviderError(
                    ErrorCategory.NOT_CONFIGURED, provider, "missing configuration")
                continue
            if self.breaker.is_open(provider):
                emit_circuit_skip(
                    request_id=request_id,
                    operation=request.operation, provider=provider,
                    seconds_remaining=self.breaker.seconds_remaining(provider),
                )
                last_error = ProviderError(
                    ErrorCategory.OUTAGE, provider,
                    "circuit open (%s)" % self.breaker.reason(provider))
                continue

            providers_tried += 1
            provider_attempt = 0
            while provider_attempt < MAX_ATTEMPTS_PER_PROVIDER:
                provider_attempt += 1
                attempts += 1
                result = None
                # Per-attempt clock. Measuring from the start of the request
                # would charge the second provider for the first one's timeout,
                # making every fallback look slow and hiding which provider is
                # actually the slow one.
                attempt_started = time.time()
                try:
                    result = adapter.generate(request)
                    result.attempt = attempts
                    if finalize is not None:
                        finalize(result)
                except ProviderError as e:
                    last_error = e
                    # Every billed failure must still report what it cost.
                    # Two ways a failure carries usage:
                    #   * the response arrived and THEN failed validation
                    #     (`result` is set), or
                    #   * the adapter rejected it — refusal, truncation, empty,
                    #     unparseable — and attached the usage to the error.
                    # Both generated tokens. Recording either as free would
                    # make the most expensive failures invisible, and these are
                    # exactly the ones that go on to pay a second provider too.
                    failed_usage = result.usage if result is not None else e.usage
                    failed_model = (result.model if result is not None
                                    else e.model or getattr(adapter, "model", "") or "")
                    if failed_usage is not None:
                        total_cost += estimate_cost(failed_model, failed_usage)
                    emit_attempt(
                        request_id=request_id,
                        operation=request.operation, provider=provider,
                        model=failed_model,
                        routing_mode=self.mode, attempt=attempts,
                        # This attempt's own duration. The adapter's figure is
                        # preferred when it has one; the local clock covers the
                        # translated SDK errors, which have no latency of their
                        # own.
                        latency_ms=(result.latency_ms if result is not None
                                    else e.latency_ms
                                    if e.latency_ms is not None
                                    else int((time.time() - attempt_started) * 1000)),
                        success=False, usage=failed_usage,
                        reasoning=getattr(adapter, "reasoning", None),
                        error_category=e.category,
                        refusal=e.category == ErrorCategory.REFUSAL,
                        incomplete=e.category == ErrorCategory.INCOMPLETE,
                    )
                    self.breaker.record_failure(provider, e)
                    # One more roll of the dice, but only for output that came
                    # back wrong. A 500 or a 401 would answer identically.
                    if e.retryable_same_provider and provider_attempt < MAX_ATTEMPTS_PER_PROVIDER:
                        continue
                    break
                except Exception as e:  # noqa: BLE001 — an adapter bug, not a provider fault
                    # INTERNAL, not UNKNOWN: this is our code failing, and it
                    # would fail identically at the next provider. Paying two
                    # more models to rediscover our own TypeError is how a bug
                    # becomes a bill.
                    last_error = ProviderError(
                        ErrorCategory.INTERNAL, provider, type(e).__name__)
                    logger.exception("llm adapter %s raised an unexpected error", provider)
                    emit_attempt(
                        request_id=request_id,
                        operation=request.operation, provider=provider,
                        model=getattr(adapter, "model", "") or "",
                        routing_mode=self.mode, attempt=attempts,
                        latency_ms=int((time.time() - attempt_started) * 1000),
                        success=False, error_category=ErrorCategory.INTERNAL,
                    )
                    break

                cost = estimate_cost(result.model, result.usage)
                total_cost += cost
                emit_attempt(
                    request_id=request_id,
                    operation=request.operation, provider=provider, model=result.model,
                    routing_mode=self.mode, attempt=attempts,
                    latency_ms=result.latency_ms, success=True,
                    usage=result.usage, reasoning=result.reasoning,
                )
                self.breaker.record_success(provider)
                emit_usage(
                    request_id=request_id,
                    operation=request.operation, final_provider=provider,
                    final_model=result.model, routing_mode=self.mode,
                    attempts=attempts, providers_tried=providers_tried,
                    total_cost_usd=total_cost,
                    total_latency_ms=int((time.time() - started) * 1000),
                    # A DIFFERENT provider answered — not merely a second
                    # attempt. A same-provider retry is two attempts at one
                    # provider and must not read as a fallback.
                    success=True, fell_back=providers_tried > 1,
                )
                return result

            # This provider is done. Whether anyone else gets a turn depends
            # entirely on the category — a refusal ends the request here.
            if last_error is not None and not last_error.eligible_for_fallback:
                break
            if index + 1 < len(chain) and last_error is not None:
                emit_fallback(
                    request_id=request_id,
                    operation=request.operation, from_provider=provider,
                    to_provider=chain[index + 1], error_category=last_error.category,
                )

        emit_usage(
            request_id=request_id,
            operation=request.operation, final_provider=None, final_model=None,
            routing_mode=self.mode, attempts=attempts, providers_tried=providers_tried,
            total_cost_usd=total_cost,
            total_latency_ms=int((time.time() - started) * 1000),
            success=False, fell_back=providers_tried > 1,
            error_category=last_error.category if last_error else None,
        )
        raise last_error or ProviderError(
            ErrorCategory.UNKNOWN, "router", "no provider produced a result")
