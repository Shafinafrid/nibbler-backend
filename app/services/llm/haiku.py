"""
Haiku — Anthropic Claude Haiku 4.5. Luna's first fallback, and the model that
served every Nibbler session before this refactor.

Two deliberate choices here.

**Structured output is a forced tool call, not a prompted JSON blob.** The old
implementation asked for JSON in the prompt and then stripped markdown fences
off the reply with a regex. That worked, but it made malformed output a routine
event rather than an exceptional one. Handing the schema to Anthropic as a tool
and setting `tool_choice` to that tool makes the model fill in a structure
instead of writing a document that resembles one. The loose parser is still
wired up as a fallback for the case where a reply arrives as text anyway.

**Sonnet is not reachable from here.** The model is validated as Haiku on every
call. The old `claude_model_paid` / `claude_model_free` split is gone: model
choice is a routing decision now, never a subscription-tier decision.

Prompt caching is preserved. The stable system block carries `cache_control`
and the per-request context block does not — the cache is a PREFIX cache, so
putting the changing bytes second is what keeps the cached prefix stable.
"""

import logging
import time
from typing import Any, Dict, Optional

from .base import LLMRequest, LLMResult, ProviderAdapter
from .errors import ErrorCategory, ProviderError, classify_status
from .usage import Usage
from .validation import parse_json_loose

logger = logging.getLogger(__name__)

NAME = "haiku"

# Exact ids, not a substring. `"haiku" in model` accepted `claude-haiku-3`, an
# older and materially weaker model. Shared with startup validation so the
# runtime check cannot be weaker than the boot check.
ALLOWED_HAIKU_MODELS = frozenset({"claude-haiku-4-5"})


class HaikuAdapter(ProviderAdapter):
    name = NAME

    def __init__(self, settings, client=None):
        self._settings = settings
        self.model = settings.anthropic_llm_model
        self._timeout = settings.anthropic_llm_timeout_seconds
        self._client = client

    # ── configuration ────────────────────────────────────────────────────

    def _api_key(self) -> str:
        """The new name wins; the legacy one keeps an existing deploy alive.

        `CLAUDE_API_KEY` is what is set in Railway today. Requiring the rename
        before this ships would mean a deploy that boots without a working
        fallback provider, so both names are read.
        """
        return self._settings.anthropic_llm_api_key or self._settings.claude_api_key

    def is_configured(self) -> bool:
        return bool(self._api_key() and self.model)

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as e:  # pragma: no cover
                raise ProviderError(ErrorCategory.NOT_CONFIGURED, NAME,
                                    "anthropic SDK not installed (%s)" % e)
            self._client = anthropic.Anthropic(
                api_key=self._api_key(), timeout=self._timeout,
                # ⚠️ The SDK default is 2 SILENT RETRIES — three billed calls
                # behind one logged attempt. The router owns retry policy so
                # that what we spend is what we count.
                max_retries=0,
            )
        return self._client

    # ── request construction ─────────────────────────────────────────────

    def _build_kwargs(self, request: LLMRequest) -> Dict[str, Any]:
        system = [{
            "type": "text",
            "text": request.system,
            # ~10% of input price on repeat calls within the TTL. Only the
            # stable block is marked: caching the per-book context would write
            # a new cache entry on every single request and cost more.
            "cache_control": {"type": "ephemeral"},
        }]
        if request.context:
            system.append({"type": "text", "text": request.context})

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_visible_tokens,
            "system": system,
            "messages": [{"role": m["role"], "content": m["content"]} for m in request.messages],
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.json_schema is not None:
            tool_name = request.schema_name or "response"
            kwargs["tools"] = [{
                "name": tool_name,
                "description": "Return the response using exactly this structure.",
                "input_schema": request.json_schema,
            }]
            # Forced: without this the model may answer in prose and ignore
            # the tool entirely.
            kwargs["tool_choice"] = {"type": "tool", "name": tool_name}
        return kwargs

    # ── response handling ────────────────────────────────────────────────

    @staticmethod
    def _extract_usage(response: Any) -> Usage:
        u = getattr(response, "usage", None)
        if u is None:
            return Usage()
        out = int(getattr(u, "output_tokens", 0) or 0)
        return Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            cached_input_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
            output_tokens=out,
            # Haiku has no hidden reasoning: everything generated is visible.
            visible_output_tokens=out,
            reasoning_tokens=0,
        )

    def _fail(self, category: str, detail: str, usage, latency_ms: int) -> ProviderError:
        """A failure that still reports what it cost — refusals and truncated
        answers are billed like any other generation."""
        return ProviderError(category, NAME, detail, usage=usage,
                             model=self.model, latency_ms=latency_ms)

    def _check_stop_reason(self, response: Any, usage, latency_ms: int) -> None:
        stop = getattr(response, "stop_reason", None)
        if stop == "max_tokens":
            raise self._fail(ErrorCategory.INCOMPLETE,
                             "hit max_tokens before finishing", usage, latency_ms)
        if stop == "refusal":
            raise self._fail(ErrorCategory.REFUSAL,
                             "model declined the request", usage, latency_ms)

    @staticmethod
    def _extract_content(response: Any, wants_json: bool):
        """Tool input if the tool was used, otherwise the text block."""
        tool_input = None
        text_parts = []
        for block in (getattr(response, "content", None) or []):
            btype = getattr(block, "type", None)
            if btype == "tool_use":
                tool_input = getattr(block, "input", None)
            elif btype == "text":
                text_parts.append(getattr(block, "text", "") or "")
        text = "".join(text_parts).strip()

        if wants_json:
            if isinstance(tool_input, dict):
                return tool_input, None
            # The model answered in prose despite tool_choice. Recoverable —
            # the old prompted-JSON path did exactly this every time.
            return parse_json_loose(text, NAME), None
        return None, text

    # ── the call ─────────────────────────────────────────────────────────

    def generate(self, request: LLMRequest) -> LLMResult:
        if self.model not in ALLOWED_HAIKU_MODELS:
            raise ProviderError(
                ErrorCategory.NOT_CONFIGURED, NAME,
                "model %r is not an approved Haiku 4.5 model — Sonnet is not used "
                "by this service, and older Haiku generations are not approved" % self.model,
            )

        client = self._get_client()
        started = time.time()
        try:
            response = client.messages.create(**self._build_kwargs(request))
        except Exception as e:
            raise _translate(e)
        latency_ms = int((time.time() - started) * 1000)

        usage = self._extract_usage(response)
        self._check_stop_reason(response, usage, latency_ms)
        try:
            data, text = self._extract_content(response, request.wants_json)
        except ProviderError as e:
            e.usage, e.model, e.latency_ms = usage, self.model, latency_ms
            raise
        if not request.wants_json and not (text or "").strip():
            raise self._fail(ErrorCategory.EMPTY_RESPONSE, "no text content", usage, latency_ms)

        return LLMResult(
            provider=NAME, model=self.model, usage=usage, latency_ms=latency_ms,
            data=data, text=text,
        )


def _translate(exc: Exception) -> ProviderError:
    """Anthropic SDK exception → normalized ProviderError."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return ProviderError(ErrorCategory.UNKNOWN, NAME, type(exc).__name__)

    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderError(ErrorCategory.TIMEOUT, NAME, "request timed out")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderError(ErrorCategory.TRANSPORT, NAME, type(exc).__name__)
    if isinstance(exc, anthropic.RateLimitError):
        msg = str(getattr(exc, "message", "") or exc)
        return ProviderError(
            classify_status(429, msg, NAME), NAME, "rate limited", status_code=429,
        )
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        msg = str(getattr(exc, "message", "") or exc)
        return ProviderError(
            classify_status(status, msg, NAME), NAME,
            "HTTP %s" % status, status_code=status,
        )
    if isinstance(exc, anthropic.APIError):
        return ProviderError(ErrorCategory.UNKNOWN, NAME, type(exc).__name__)
    return ProviderError(ErrorCategory.UNKNOWN, NAME, type(exc).__name__)
