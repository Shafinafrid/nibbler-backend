"""
One error vocabulary for three providers.

The router has exactly one decision to make when a provider fails: try the next
one, or stop. That decision must not depend on which SDK raised, so every
provider exception is translated here into a `ProviderError` carrying a
`category`, and the categories — not the original exception types — decide
whether the chain continues.

Two rules shape the taxonomy:

  * A SAFETY REFUSAL IS NOT A FAILURE TO ROUTE AROUND. If a model declines on
    content grounds, asking a cheaper model the same question until one answers
    is exactly the behaviour that must not exist. `REFUSAL` is ineligible.
  * OUR auth failing and THEIR auth failing are opposite events. A rejected
    Nibbler user token must never reach a model at all; a rejected provider API
    key should fall through to the next provider and open that provider's
    breaker. They are never the same category.

Classification prefers official SDK exception types and HTTP status codes.
Message-substring matching is a last resort, used only to separate billing and
quota problems from generic 4xx — the SDKs surface both as the same class.
"""

from typing import Optional


class ErrorCategory:
    """Normalized provider failure categories.

    Plain string constants rather than an Enum: they are logged, compared and
    written into test fixtures, and this repo is on Python 3.9 where StrEnum
    does not exist.
    """

    # ── eligible for fallback ────────────────────────────────────────────
    INSUFFICIENT_CREDIT = "insufficient_credit"
    BILLING = "billing"
    QUOTA = "quota"
    RATE_LIMIT = "rate_limit"
    PROVIDER_AUTH = "provider_auth"        # THEIR key is bad — not the user's
    OUTAGE = "outage"                      # 5xx
    TIMEOUT = "timeout"
    TRANSPORT = "transport"                # DNS, refused connection, tunnel down
    EMPTY_RESPONSE = "empty_response"
    INCOMPLETE = "incomplete"              # ran out of output tokens mid-answer
    SCHEMA = "schema"                      # unparseable / schema-invalid output
    VALIDATION = "validation"              # parsed, but semantically wrong

    # ── NOT eligible for fallback ────────────────────────────────────────
    REFUSAL = "refusal"                    # model declined on content grounds
    BAD_REQUEST = "bad_request"            # our parameters are wrong — a bug
    NOT_CONFIGURED = "not_configured"      # provider disabled or missing config
    INTERNAL = "internal"                  # OUR code raised — an adapter bug
    UNKNOWN = "unknown"                    # unclassified; treated as ours, not theirs


# Everything here may advance the chain to the next provider. Anything absent
# stops it. Written as an explicit allow-list so a category added later is
# ineligible until someone deliberately decides otherwise.
#
# ⚠️ UNKNOWN AND INTERNAL ARE DELIBERATELY ABSENT (added 2026-08-02 after audit).
# They used to be eligible, which meant a TypeError in our own adapter code —
# a deterministic bug that every provider would hit identically — bought a
# second and third paid call before surfacing. An unclassified failure is
# treated as ours until proven otherwise: every real provider failure mode
# below has an explicit category, so landing in UNKNOWN means the classifier
# missed something, and the safe response to "I don't know what went wrong" is
# to stop rather than to spend.
ELIGIBLE_FOR_FALLBACK = frozenset({
    ErrorCategory.INSUFFICIENT_CREDIT,
    ErrorCategory.BILLING,
    ErrorCategory.QUOTA,
    ErrorCategory.RATE_LIMIT,
    ErrorCategory.PROVIDER_AUTH,
    ErrorCategory.OUTAGE,
    ErrorCategory.TIMEOUT,
    ErrorCategory.TRANSPORT,
    ErrorCategory.EMPTY_RESPONSE,
    ErrorCategory.INCOMPLETE,
    ErrorCategory.SCHEMA,
    ErrorCategory.VALIDATION,
})

# Categories that mean "this provider is unwell", not "this request was
# unlucky" — the breaker opens on these so the next N requests skip it instead
# of each paying the same timeout or the same 401.
CIRCUIT_OPENING = frozenset({
    ErrorCategory.INSUFFICIENT_CREDIT,
    ErrorCategory.BILLING,
    ErrorCategory.QUOTA,
    ErrorCategory.PROVIDER_AUTH,
    ErrorCategory.OUTAGE,
    ErrorCategory.TRANSPORT,
    ErrorCategory.RATE_LIMIT,
})

# Bad output is worth one more roll of the dice at the same provider —
# temperature alone often fixes it, and a second provider is a bigger change
# than a second attempt. Everything else goes straight to the next provider.
RETRYABLE_SAME_PROVIDER = frozenset({
    ErrorCategory.SCHEMA,
    ErrorCategory.VALIDATION,
    ErrorCategory.EMPTY_RESPONSE,
})


class ProviderError(Exception):
    """A normalized provider failure.

    `detail` is for logs and must stay free of prompt content — it is built
    from exception types and status codes, never from the text we sent.

    `usage` and `model` carry the cost of a failure that ALREADY BURNED TOKENS.
    A refusal, a truncated answer and an empty completion are all billed: the
    provider generated something, we just cannot use it. Raising without them
    made a failed request look free, which is the opposite of true — those are
    exactly the requests that go on to pay a second provider as well.
    """

    def __init__(
        self,
        category: str,
        provider: str,
        detail: str = "",
        status_code: Optional[int] = None,
        usage=None,
        model: Optional[str] = None,
        latency_ms: Optional[int] = None,
    ):
        super().__init__("%s/%s: %s" % (provider, category, detail))
        self.category = category
        self.provider = provider
        self.detail = detail
        self.status_code = status_code
        self.usage = usage
        self.model = model
        self.latency_ms = latency_ms

    @property
    def eligible_for_fallback(self) -> bool:
        return self.category in ELIGIBLE_FOR_FALLBACK

    @property
    def opens_circuit(self) -> bool:
        return self.category in CIRCUIT_OPENING

    @property
    def retryable_same_provider(self) -> bool:
        return self.category in RETRYABLE_SAME_PROVIDER


# Substrings that distinguish a spend problem from any other 4xx. Both OpenAI
# and Anthropic report exhausted credit as a plain 400/403, so the status code
# alone cannot tell "you are out of money" from "your request was malformed" —
# and those two want opposite handling (open the breaker vs. fix the bug).
# Kept deliberately small; the status code does the real work.
_BILLING_HINTS = (
    "insufficient_quota",
    "insufficient credit",
    "insufficient_credit",
    "credit balance",
    "billing",
    "payment",
    "exceeded your current quota",
    "purchase",
)


def _has_billing_hint(text: str) -> bool:
    low = (text or "").lower()
    return any(h in low for h in _BILLING_HINTS)


def classify_status(status: Optional[int], message: str, provider: str) -> str:
    """Category for an HTTP status code, with billing hints applied to 4xx."""
    if status is None:
        return ErrorCategory.UNKNOWN
    if status == 429:
        # 429 is overloaded: real rate limiting, or a hard quota/credit wall.
        # Both are eligible, but only the latter should keep the breaker open
        # for long, so they stay separate categories.
        return ErrorCategory.QUOTA if _has_billing_hint(message) else ErrorCategory.RATE_LIMIT
    if status in (401, 403):
        return ErrorCategory.BILLING if _has_billing_hint(message) else ErrorCategory.PROVIDER_AUTH
    if status == 402:
        return ErrorCategory.INSUFFICIENT_CREDIT
    if status >= 500:
        return ErrorCategory.OUTAGE
    if status == 408:
        return ErrorCategory.TIMEOUT
    if status >= 400:
        if _has_billing_hint(message):
            return ErrorCategory.INSUFFICIENT_CREDIT
        # A 4xx that is not about money or auth is our own malformed request —
        # a deterministic bug that every other provider would reject too.
        return ErrorCategory.BAD_REQUEST
    return ErrorCategory.UNKNOWN
