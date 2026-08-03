"""
Luna — OpenAI GPT-5.6 Luna, via the Responses API. The default provider.

Three things about this model drive the whole adapter:

1. **The model id must be exact.** `gpt-5.6` is an alias that routes to Sol, a
   different and more expensive model. Only `gpt-5.6-luna` is Luna, so the id
   is validated rather than trusted.

2. **Reasoning is hidden but billed as output, and it eats the output budget.**
   `max_output_tokens` covers reasoning tokens AND the visible answer. Passing
   the old Anthropic `max_tokens` straight through would let a low-effort
   reasoning pass consume the allowance and truncate the JSON halfway — which
   surfaces as `status == "incomplete"`, not as an error. Hence
   `_total_output_budget()`, and hence incomplete responses being a hard
   failure rather than a partial result.

3. **There are two independent reasoning knobs.** `effort` (none → max) and
   `mode` (standard | pro). Capping effort does NOT cap mode: `pro` is a
   separate, pricier execution path. Both are pinned here.

Refusals are surfaced as their own error category and are NOT eligible for
fallback — asking a cheaper model the same question until one answers is
exactly the behaviour that must not exist.
"""

import logging
import time
from typing import Any, Dict, Optional

from .base import LLMRequest, LLMResult, ProviderAdapter
from .errors import ErrorCategory, ProviderError, classify_status
from .usage import Usage
from .validation import parse_json_loose

logger = logging.getLogger(__name__)

NAME = "luna"

# The reasoning efforts this implementation permits. `minimal` sits between
# `none` and `low` in OpenAI's ladder, so it is under the cap and allowed;
# medium, high, xhigh and max are all above it and rejected.
ALLOWED_REASONING_EFFORTS = ("none", "minimal", "low")

# Exact ids, not a prefix. `startswith("gpt-5.6-luna")` accepted
# `gpt-5.6-luna-preview` — a different model with different pricing and
# behaviour, which is the substitution the pinning exists to prevent. Lives
# here rather than in router.py so the RUNTIME check and the STARTUP check
# share one list: a deployment that somehow skips validation must still be
# unable to call an unapproved model.
ALLOWED_LUNA_MODELS = frozenset({"gpt-5.6-luna"})

# Headroom added on top of the visible budget to cover hidden reasoning.
# Low effort is a short pass, but "short" is not "bounded", so this is
# generous: an incomplete response wastes the whole call, while unused budget
# costs nothing (output is billed on tokens produced, not tokens allowed).
REASONING_HEADROOM_MIN = 2_000
REASONING_HEADROOM_RATIO = 1.0


def _total_output_budget(visible: int) -> int:
    """Total generated-token allowance for a given visible-answer size.

    Not the provider maximum — a flat 128K would remove the ceiling that makes
    a runaway generation visible — and never the bare visible size either.
    """
    return int(visible + max(REASONING_HEADROOM_MIN, visible * REASONING_HEADROOM_RATIO))


class LunaAdapter(ProviderAdapter):
    name = NAME

    def __init__(self, settings, client=None):
        self._settings = settings
        self.model = settings.openai_llm_model
        self.reasoning = settings.openai_llm_reasoning_effort
        self._timeout = settings.openai_llm_timeout_seconds
        self._client = client  # injected in tests; never constructed eagerly

    # ── configuration ────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        return bool(self._settings.openai_llm_api_key and self.model)

    def _get_client(self):
        """Lazily build the SDK client. No network call, no paid call — this
        exists so a Haiku-only deployment never needs an OpenAI package that
        works, let alone an OpenAI key that does."""
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover — dependency missing
                raise ProviderError(ErrorCategory.NOT_CONFIGURED, NAME,
                                    "openai SDK not installed (%s)" % e)
            self._client = OpenAI(
                api_key=self._settings.openai_llm_api_key,
                timeout=self._timeout,
                # ⚠️ The SDK default is 2 SILENT RETRIES. Left alone, one
                # "attempt" in our telemetry could be three billed HTTP calls,
                # and a three-provider chain up to seven — none of them visible
                # in llm_attempt. Retry policy belongs to the router, which
                # counts and logs what it spends.
                max_retries=0,
            )
        return self._client

    # ── request construction ─────────────────────────────────────────────

    def _build_kwargs(self, request: LLMRequest) -> Dict[str, Any]:
        # The stable system text goes in `instructions` and the dynamic context
        # becomes a leading user turn. Keeping them apart is what lets the
        # prefix stay cacheable across calls.
        input_items = []
        if request.context:
            input_items.append({"role": "user", "content": request.context})
        for m in request.messages:
            input_items.append({"role": m["role"], "content": m["content"]})

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "instructions": request.system,
            "input": input_items,
            "max_output_tokens": _total_output_budget(request.max_visible_tokens),
            # `mode` pinned to standard: pro is a separate, more expensive
            # execution path and is orthogonal to the effort cap above it.
            "reasoning": {"effort": self.reasoning, "mode": "standard"},
        }
        if request.json_schema is not None:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name or "response",
                    "schema": request.json_schema,
                    "strict": True,
                }
            }
        return kwargs

    # ── response handling ────────────────────────────────────────────────

    @staticmethod
    def _extract_usage(response: Any) -> Usage:
        u = getattr(response, "usage", None)
        if u is None:
            return Usage()
        details = getattr(u, "input_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
        out_details = getattr(u, "output_tokens_details", None)
        reasoning = int(getattr(out_details, "reasoning_tokens", 0) or 0) if out_details else 0
        total_out = int(getattr(u, "output_tokens", 0) or 0)
        return Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            cached_input_tokens=cached,
            # The Responses API does not report cache writes separately; the
            # cached-read count is what it exposes.
            cache_write_tokens=0,
            output_tokens=total_out,
            visible_output_tokens=max(0, total_out - reasoning),
            reasoning_tokens=reasoning,
        )

    def _fail(self, category: str, detail: str, usage: Usage, latency_ms: int) -> ProviderError:
        """Build a failure that still carries what it cost.

        Refusals, truncations and empty completions are all BILLED — the model
        generated tokens, we just cannot use them. Dropping the usage here
        would make the most expensive failure mode look free.
        """
        return ProviderError(category, NAME, detail, usage=usage,
                             model=self.model, latency_ms=latency_ms)

    @staticmethod
    def _refusal_text(response: Any) -> Optional[str]:
        """A refusal arrives as a content part, not an exception."""
        for item in (getattr(response, "output", None) or []):
            if getattr(item, "type", None) != "message":
                continue
            for part in (getattr(item, "content", None) or []):
                if getattr(part, "type", None) == "refusal":
                    return getattr(part, "refusal", None) or "declined"
        return None

    # ── the call ─────────────────────────────────────────────────────────

    def generate(self, request: LLMRequest) -> LLMResult:
        if self.reasoning not in ALLOWED_REASONING_EFFORTS:
            # Defence in depth: startup validation rejects this too, but a
            # runtime check means no code path can quietly raise the effort.
            raise ProviderError(
                ErrorCategory.NOT_CONFIGURED, NAME,
                "reasoning effort %r exceeds the 'low' cap" % self.reasoning,
            )
        if self.model not in ALLOWED_LUNA_MODELS:
            raise ProviderError(
                ErrorCategory.NOT_CONFIGURED, NAME,
                "model %r is not an approved Luna model (the bare gpt-5.6 alias "
                "routes to Sol; -preview variants are not approved)" % self.model,
            )

        client = self._get_client()
        started = time.time()
        try:
            response = client.responses.create(**self._build_kwargs(request))
        except Exception as e:
            raise _translate(e)
        latency_ms = int((time.time() - started) * 1000)

        # Usage first: everything below can fail, and every one of those
        # failures has already been paid for.
        usage = self._extract_usage(response)

        refusal = self._refusal_text(response)
        if refusal is not None:
            raise self._fail(ErrorCategory.REFUSAL, "model declined the request",
                             usage, latency_ms)

        # Truncation is reported as a status, not an error. Treated as a hard
        # failure: half a JSON deck is not a partial success, and persisting it
        # would ship a broken session.
        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            reason = getattr(details, "reason", None) or "unknown"
            raise self._fail(ErrorCategory.INCOMPLETE,
                             "response incomplete (%s)" % reason, usage, latency_ms)

        text = (getattr(response, "output_text", None) or "").strip()
        if not text:
            raise self._fail(ErrorCategory.EMPTY_RESPONSE, "no output text", usage, latency_ms)

        data: Optional[Dict[str, Any]] = None
        if request.wants_json:
            # Strict mode should make this exact, but the loose parser costs
            # nothing and covers the case where a future model wraps its JSON.
            try:
                data = parse_json_loose(text, NAME)
            except ProviderError as e:
                e.usage, e.model, e.latency_ms = usage, self.model, latency_ms
                raise

        return LLMResult(
            provider=NAME, model=self.model, usage=usage, latency_ms=latency_ms,
            data=data, text=None if request.wants_json else text,
            reasoning=self.reasoning,
        )


def _translate(exc: Exception) -> ProviderError:
    """OpenAI SDK exception → normalized ProviderError.

    Uses the SDK's own exception classes first and the HTTP status second;
    message text is consulted only to separate billing from other 4xx, which
    the status code genuinely cannot do.
    """
    try:
        import openai
    except ImportError:  # pragma: no cover
        return ProviderError(ErrorCategory.UNKNOWN, NAME, type(exc).__name__)

    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(ErrorCategory.TIMEOUT, NAME, "request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(ErrorCategory.TRANSPORT, NAME, type(exc).__name__)
    if isinstance(exc, openai.RateLimitError):
        msg = str(getattr(exc, "message", "") or exc)
        return ProviderError(
            classify_status(429, msg, NAME), NAME, "rate limited", status_code=429,
        )
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        msg = str(getattr(exc, "message", "") or exc)
        return ProviderError(
            classify_status(status, msg, NAME), NAME,
            "HTTP %s" % status, status_code=status,
        )
    if isinstance(exc, openai.APIError):
        return ProviderError(ErrorCategory.UNKNOWN, NAME, type(exc).__name__)
    return ProviderError(ErrorCategory.UNKNOWN, NAME, type(exc).__name__)
