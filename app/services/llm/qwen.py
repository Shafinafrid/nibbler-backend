"""
Qwen — a self-hosted model behind a configurable OpenAI-compatible endpoint.
The second fallback.

The backend never loads a model here. Qwen is an HTTP provider like any other:
the 14B weights live on the owner's MacBook under `llama-server`, and Railway
reaches it over an authenticated HTTPS tunnel. Nothing in this file imports
torch, downloads weights, or allocates GPU memory — Railway's container could
not host a 9 GB model even if it tried, and a `pip install` that implied
otherwise would be a deployment hazard.

**Railway cannot reach a MacBook's localhost.** `127.0.0.1` inside a Railway
container is the container. So `QWEN_BASE_URL` must be a public HTTPS tunnel
URL in production; a loopback or private-LAN address is only valid when the
backend is running on the same machine, and is warned about rather than
rejected so local development still works.

Because the transport is the fragile part — the Mac sleeps, the tunnel drops,
llama-server gets quit — every connection failure is classified as TRANSPORT,
which is eligible for fallback and opens the circuit breaker. A sleeping laptop
should cost one request, not every request.

The model runs in NON-THINKING mode. Qwen3 toggles this from the prompt with
`/no_think`, and a thinking pass here would spend the output budget on
reasoning the product never displays.
"""

import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .base import LLMRequest, LLMResult, ProviderAdapter
from .errors import ErrorCategory, ProviderError, classify_status
from .usage import Usage
from .validation import parse_json_loose

logger = logging.getLogger(__name__)

NAME = "qwen"

# Qwen3's chat template reads this token and skips the thinking block.
NO_THINK = "/no_think"

_LOCAL_HOST_PREFIXES = ("localhost", "127.", "0.0.0.0", "192.168.", "10.", "::1")


def is_local_url(url: str) -> bool:
    """True for an address a Railway container could not route to the Mac.

    Used for a startup warning, not a hard rejection: running the backend
    locally against a local llama-server is a legitimate setup.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host.startswith("172."):
        # 172.16.0.0/12 is private; 172.32+ is not.
        try:
            second = int(host.split(".")[1])
            return 16 <= second <= 31
        except (IndexError, ValueError):
            return False
    return any(host.startswith(p) for p in _LOCAL_HOST_PREFIXES)


class QwenAdapter(ProviderAdapter):
    name = NAME

    def __init__(self, settings, client=None):
        self._settings = settings
        self.model = settings.qwen_model
        self.base_url = settings.qwen_base_url
        self._timeout = settings.qwen_timeout_seconds
        self._client = client

    # ── configuration ────────────────────────────────────────────────────

    def is_configured(self) -> bool:
        # The key is required even though a local server could run open: the
        # endpoint is on the public internet through a tunnel, and an
        # unauthenticated one is a free GPU for anyone who finds the URL.
        return bool(self.base_url and self.model and self._settings.qwen_api_key)

    def _get_client(self):
        """An OpenAI client pointed somewhere else.

        Reusing the official SDK rather than hand-rolling httpx calls means the
        retry, timeout and error-class behaviour is identical to Luna's, so
        `_translate` can be shared logic rather than a second parallel guess at
        what a 429 means.
        """
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover
                raise ProviderError(ErrorCategory.NOT_CONFIGURED, NAME,
                                    "openai SDK not installed (%s)" % e)
            self._client = OpenAI(
                api_key=self._settings.qwen_api_key,
                base_url=self.base_url,
                timeout=self._timeout,
                max_retries=0,  # the router owns retry policy, not the SDK
            )
        return self._client

    # ── request construction ─────────────────────────────────────────────

    def _build_kwargs(self, request: LLMRequest) -> Dict[str, Any]:
        system_text = request.system + "\n\n" + NO_THINK
        messages = [{"role": "system", "content": system_text}]
        if request.context:
            messages.append({"role": "system", "content": request.context})
        for m in request.messages:
            messages.append({"role": m["role"], "content": m["content"]})

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": request.max_visible_tokens,
        }
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        if request.json_schema is not None:
            # llama-server converts a JSON Schema into a GBNF grammar and
            # constrains decoding with it, so this is genuine structured
            # output rather than a request the model may ignore.
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name or "response",
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
        return kwargs

    # ── response handling ────────────────────────────────────────────────

    @staticmethod
    def _extract_usage(response: Any) -> Usage:
        u = getattr(response, "usage", None)
        if u is None:
            return Usage()
        out = int(getattr(u, "completion_tokens", 0) or 0)
        return Usage(
            input_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
            cached_input_tokens=0,
            cache_write_tokens=0,
            output_tokens=out,
            visible_output_tokens=out,
            reasoning_tokens=0,
        )

    def _fail(self, category: str, detail: str, usage, latency_ms: int) -> ProviderError:
        """A failure that still reports the tokens it burned."""
        return ProviderError(category, NAME, detail, usage=usage,
                             model=self.model, latency_ms=latency_ms)

    # ── the call ─────────────────────────────────────────────────────────

    def generate(self, request: LLMRequest) -> LLMResult:
        if not self.base_url:
            raise ProviderError(ErrorCategory.NOT_CONFIGURED, NAME, "no QWEN_BASE_URL")

        client = self._get_client()
        started = time.time()
        try:
            response = client.chat.completions.create(**self._build_kwargs(request))
        except Exception as e:
            raise _translate(e)
        latency_ms = int((time.time() - started) * 1000)

        usage = self._extract_usage(response)
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise self._fail(ErrorCategory.EMPTY_RESPONSE, "no choices returned",
                             usage, latency_ms)
        choice = choices[0]
        message = getattr(choice, "message", None)
        finish = getattr(choice, "finish_reason", None)

        # A refusal must be recognised as a refusal, not mistaken for an empty
        # answer. The distinction decides whether the router stops or keeps
        # asking: treating a decline as "empty" would retry it here and then
        # hand the same request to the next provider, which is precisely the
        # shop-for-a-compliant-model behaviour the chain must not have.
        refusal = getattr(message, "refusal", None) if message is not None else None
        if refusal:
            raise self._fail(ErrorCategory.REFUSAL, "model declined the request",
                             usage, latency_ms)
        if finish in ("content_filter", "refusal"):
            raise self._fail(ErrorCategory.REFUSAL,
                             "stopped by content filter (%s)" % finish, usage, latency_ms)
        if finish == "length":
            raise self._fail(ErrorCategory.INCOMPLETE,
                             "hit max_tokens before finishing", usage, latency_ms)

        text = (getattr(message, "content", None) or "").strip() if message is not None else ""
        if not text:
            raise self._fail(ErrorCategory.EMPTY_RESPONSE, "empty message content",
                             usage, latency_ms)

        data: Optional[Dict[str, Any]] = None
        if request.wants_json:
            try:
                data = parse_json_loose(text, NAME)
            except ProviderError as e:
                e.usage, e.model, e.latency_ms = usage, self.model, latency_ms
                raise

        return LLMResult(
            provider=NAME, model=self.model, usage=usage, latency_ms=latency_ms,
            data=data, text=None if request.wants_json else text,
        )


def _translate(exc: Exception) -> ProviderError:
    """OpenAI-SDK exception from a self-hosted endpoint → ProviderError.

    Connection failures dominate here and all mean the same thing: the Mac,
    the tunnel or llama-server is not there right now.
    """
    try:
        import openai
    except ImportError:  # pragma: no cover
        return ProviderError(ErrorCategory.TRANSPORT, NAME, type(exc).__name__)

    if isinstance(exc, openai.APITimeoutError):
        return ProviderError(ErrorCategory.TIMEOUT, NAME, "request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderError(ErrorCategory.TRANSPORT, NAME,
                             "endpoint unreachable (%s)" % type(exc).__name__)
    if isinstance(exc, openai.RateLimitError):
        return ProviderError(ErrorCategory.RATE_LIMIT, NAME, "rate limited", status_code=429)
    if isinstance(exc, openai.APIStatusError):
        status = getattr(exc, "status_code", None)
        msg = str(getattr(exc, "message", "") or exc)
        return ProviderError(
            classify_status(status, msg, NAME), NAME,
            "HTTP %s" % status, status_code=status,
        )
    if isinstance(exc, openai.APIError):
        return ProviderError(ErrorCategory.TRANSPORT, NAME, type(exc).__name__)
    return ProviderError(ErrorCategory.UNKNOWN, NAME, type(exc).__name__)
