"""
The contract every provider adapter implements.

`LLMRequest` is deliberately shaped around what the three providers have in
common rather than around any one of them: a stable system block, an optional
dynamic context block, alternating messages, an optional JSON schema, and a
visible-output budget. An adapter's whole job is to render that into its SDK's
vocabulary and render the reply back into `LLMResult`.

The split between `system` and `context` exists for caching. `system` is the
same bytes on every call and is what providers cache; `context` changes per
request (a book's retrieved passages). Merging them would make the cacheable
prefix change every time and quietly cost real money — Anthropic caches a
prefix, not a set of blocks.

`max_visible_tokens` is the size of the ANSWER, not the request's token
allowance. Providers that hide reasoning inside the output budget have to add
their own headroom on top; see luna.py. Naming it "visible" is the guard
against the mistake of passing an Anthropic max_tokens straight to a reasoning
model, where it would be consumed before the JSON is finished.
"""

from typing import Any, Dict, List, Optional

from .usage import Usage


class LLMRequest:
    """One provider-neutral generation request."""

    __slots__ = (
        "operation", "system", "context", "messages",
        "json_schema", "schema_name", "max_visible_tokens", "temperature",
    )

    def __init__(
        self,
        *,
        operation: str,
        system: str,
        messages: List[Dict[str, str]],
        max_visible_tokens: int,
        context: Optional[str] = None,
        json_schema: Optional[Dict[str, Any]] = None,
        schema_name: Optional[str] = None,
        temperature: Optional[float] = None,
    ):
        self.operation = operation
        self.system = system
        self.context = context
        self.messages = messages
        self.json_schema = json_schema
        self.schema_name = schema_name
        self.max_visible_tokens = max_visible_tokens
        self.temperature = temperature

    @property
    def wants_json(self) -> bool:
        return self.json_schema is not None


class LLMResult:
    """One successful provider response, normalized.

    Exactly one of `data` (JSON workflows) or `text` (Connect) is populated.
    """

    __slots__ = (
        "data", "text", "usage", "provider", "model",
        "latency_ms", "attempt", "reasoning",
    )

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        usage: Usage,
        latency_ms: int,
        data: Optional[Dict[str, Any]] = None,
        text: Optional[str] = None,
        attempt: int = 1,
        reasoning: Optional[str] = None,
    ):
        self.provider = provider
        self.model = model
        self.usage = usage
        self.latency_ms = latency_ms
        self.data = data
        self.text = text
        self.attempt = attempt
        self.reasoning = reasoning


class ProviderAdapter:
    """Base class for Luna, Haiku and Qwen adapters.

    Subclasses raise `ProviderError` for every failure — including refusals and
    incomplete responses, which are failures with categories the router treats
    differently rather than a separate return path.
    """

    name = "base"
    model = ""
    reasoning = None  # only Luna reports one

    def is_configured(self) -> bool:
        """True when this provider has everything it needs to be called.

        Checked without any network access: constructing a client must not
        cost money or latency, so a provider that is enabled but unreachable
        still reports configured and fails at call time with a TRANSPORT error.
        """
        raise NotImplementedError

    def generate(self, request: LLMRequest) -> LLMResult:
        raise NotImplementedError
