"""
Routing policy: which provider is called, how often, and when the chain stops.

The properties under test are the ones whose failure costs money or misleads
a review:

  * single mode NEVER reaches a second provider — otherwise the owner compares
    "Luna's output" that Haiku actually wrote;
  * a safety refusal STOPS the chain — otherwise the fallback order becomes a
    way to shop for a model that will comply;
  * retries and fallbacks are BOUNDED — otherwise one bad afternoon multiplies
    the bill by the length of the provider list.

    .venv/bin/python tests/test_llm_routing.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_fakes import Checks, make_settings  # noqa: E402

from app.services.llm.base import LLMRequest, LLMResult  # noqa: E402
from app.services.llm.circuit import CircuitBreaker  # noqa: E402
from app.services.llm.errors import (  # noqa: E402
    ELIGIBLE_FOR_FALLBACK, ErrorCategory, ProviderError, classify_status,
)
from app.services.llm.router import (  # noqa: E402
    LLMConfigError, LLMRouter, PROVIDER_IDS, validate_llm_settings,
)
from app.services.llm.usage import Usage  # noqa: E402

c = Checks("LLM routing")


class ScriptedAdapter:
    """An adapter that plays a fixed script and counts its calls.

    Substituted for the real adapters so routing can be tested without any SDK,
    any key, or any network.
    """

    def __init__(self, name, script, model=None, configured=True):
        self.name = name
        self.model = model or ("gpt-5.6-luna" if name == "luna" else name)
        self.reasoning = "low" if name == "luna" else None
        self._script = list(script)
        self._configured = configured
        self.calls = 0

    def is_configured(self):
        return self._configured

    def generate(self, request):
        self.calls += 1
        step = self._script.pop(0) if self._script else self._script_default()
        if isinstance(step, Exception):
            raise step
        return LLMResult(
            provider=self.name, model=self.model, usage=Usage(1000, 0, 0, 500, 500, 0),
            latency_ms=10, data={"ok": True, "from": self.name}, text="hello from %s" % self.name,
        )

    def _script_default(self):
        return None


def router_with(adapters, **settings_overrides):
    settings = make_settings(**settings_overrides)
    return LLMRouter(settings, adapters=adapters, breaker=CircuitBreaker(60)), settings


def req():
    return LLMRequest(
        operation="wisdom_session", system="SYS",
        messages=[{"role": "user", "content": "hi"}], max_visible_tokens=100,
    )


def err(category, provider="luna"):
    return ProviderError(category, provider, "test")


# ── configuration validation ────────────────────────────────────────────────

c.ok(make_settings().llm_active_provider == "luna", "Luna is the default active provider")
c.ok(make_settings().llm_fallback_order == "luna,haiku,qwen", "default order is luna,haiku,qwen")
c.ok(make_settings().openai_llm_reasoning_effort == "low", "Luna reasoning defaults to low")
c.ok(make_settings().openai_llm_model == "gpt-5.6-luna", "Luna model id is explicit, not the alias")

c.ok(validate_llm_settings(make_settings()) is not None, "a fully configured deploy validates")

for bad_effort in ("medium", "high", "xhigh", "max"):
    c.raises(lambda e=bad_effort: validate_llm_settings(make_settings(openai_llm_reasoning_effort=e)),
             LLMConfigError, "reasoning effort %r is rejected" % bad_effort)
for ok_effort in ("none", "minimal", "low"):
    try:
        validate_llm_settings(make_settings(openai_llm_reasoning_effort=ok_effort))
        c.ok(True, "reasoning effort %r is allowed" % ok_effort)
    except LLMConfigError:
        c.ok(False, "reasoning effort %r is allowed" % ok_effort)

c.raises(lambda: validate_llm_settings(make_settings(openai_llm_model="gpt-5.6")),
         LLMConfigError, "the bare gpt-5.6 alias is rejected (it routes to Sol)")
c.raises(lambda: validate_llm_settings(make_settings(llm_routing_mode="turbo")),
         LLMConfigError, "unknown routing mode is rejected")
c.raises(lambda: validate_llm_settings(make_settings(llm_routing_mode="single",
                                                     llm_active_provider="gemini")),
         LLMConfigError, "unknown provider is rejected")
c.raises(lambda: validate_llm_settings(make_settings(llm_fallback_order="luna,haiku,luna")),
         LLMConfigError, "duplicate providers in the fallback order are rejected")
c.raises(lambda: validate_llm_settings(make_settings(llm_fallback_order="")),
         LLMConfigError, "empty fallback order is rejected")
c.raises(lambda: validate_llm_settings(make_settings(llm_routing_mode="single",
                                                     llm_active_provider="qwen",
                                                     llm_qwen_enabled=False)),
         LLMConfigError, "a disabled active provider is rejected")
c.raises(lambda: validate_llm_settings(make_settings(llm_haiku_enabled=False)),
         LLMConfigError, "a disabled provider listed in the fallback order is rejected")
c.raises(lambda: validate_llm_settings(make_settings(llm_luna_enabled=False,
                                                     llm_haiku_enabled=False,
                                                     llm_qwen_enabled=False,
                                                     llm_fallback_order="luna")),
         LLMConfigError, "no usable provider is rejected")

# A single-provider deployment must not demand the other providers' keys —
# that is the difference between "configurable" and "needs every account".
for provider, keep in (("luna", "openai_llm_api_key"),
                       ("haiku", "anthropic_llm_api_key"),
                       ("qwen", "qwen_api_key")):
    blanks = {k: "" for k in ("openai_llm_api_key", "anthropic_llm_api_key",
                              "qwen_api_key", "qwen_base_url") if k != keep}
    if provider == "qwen":
        blanks.pop("qwen_base_url", None)
    try:
        validate_llm_settings(make_settings(llm_routing_mode="single",
                                            llm_active_provider=provider, **blanks))
        c.ok(True, "%s-only mode needs no other provider's credentials" % provider)
    except LLMConfigError as e:
        c.ok(False, "%s-only mode needs no other provider's credentials (%s)" % (provider, e))

warnings = validate_llm_settings(make_settings(qwen_base_url="http://127.0.0.1:8080/v1"))
c.ok(any("loopback" in w or "cannot reach" in w for w in warnings),
     "a loopback QWEN_BASE_URL warns that Railway cannot reach the MacBook")

# Constructing adapters during validation must not cost anything.
from app.services.llm.router import build_adapter  # noqa: E402
c.ok(all(build_adapter(p, make_settings())._client is None for p in PROVIDER_IDS),
     "no provider client is constructed during config validation (no paid call)")


# ── single-provider mode ────────────────────────────────────────────────────

for provider in PROVIDER_IDS:
    others = [p for p in PROVIDER_IDS if p != provider]
    adapters = {p: ScriptedAdapter(p, [None]) for p in PROVIDER_IDS}
    router, _ = router_with(adapters, llm_routing_mode="single", llm_active_provider=provider)
    result = router.run(req())
    c.ok(result.provider == provider and adapters[provider].calls == 1,
         "single %s mode calls %s" % (provider, provider))
    c.ok(all(adapters[o].calls == 0 for o in others),
         "single %s mode calls nothing else" % provider)

# The property that makes the mode worth having.
adapters = {p: ScriptedAdapter(p, [err(ErrorCategory.OUTAGE, p)]) for p in PROVIDER_IDS}
router, _ = router_with(adapters, llm_routing_mode="single", llm_active_provider="luna")
c.raises(lambda: router.run(req()), ProviderError,
         "single mode does not cross to another provider even on an eligible failure")
c.ok(adapters["haiku"].calls == 0 and adapters["qwen"].calls == 0,
     "single mode left haiku and qwen untouched after Luna failed")

# One same-provider retry is still allowed in single mode — but only one.
adapters = {"luna": ScriptedAdapter("luna", [err(ErrorCategory.SCHEMA), None])}
router, _ = router_with(adapters, llm_routing_mode="single", llm_active_provider="luna")
router.run(req())
c.ok(adapters["luna"].calls == 2, "single mode retries the same provider once on bad output")

adapters = {"luna": ScriptedAdapter("luna", [err(ErrorCategory.SCHEMA)] * 5)}
router, _ = router_with(adapters, llm_routing_mode="single", llm_active_provider="luna")
c.raises(lambda: router.run(req()), ProviderError, "repeated bad output eventually fails")
c.ok(adapters["luna"].calls == 2, "the same-provider retry is bounded at 2 attempts")


# ── fallback mode ───────────────────────────────────────────────────────────

adapters = {p: ScriptedAdapter(p, [None]) for p in PROVIDER_IDS}
router, _ = router_with(adapters)
c.ok(router.provider_chain() == ["luna", "haiku", "qwen"], "default chain is Luna → Haiku → Qwen")
result = router.run(req())
c.ok(result.provider == "luna" and adapters["haiku"].calls == 0,
     "Luna success stops the chain")

for category, label in (
    (ErrorCategory.INSUFFICIENT_CREDIT, "insufficient credit"),
    (ErrorCategory.BILLING, "billing rejection"),
    (ErrorCategory.RATE_LIMIT, "rate limit"),
    (ErrorCategory.TIMEOUT, "timeout"),
    (ErrorCategory.OUTAGE, "outage"),
    (ErrorCategory.QUOTA, "quota exhaustion"),
    (ErrorCategory.PROVIDER_AUTH, "provider credential rejection"),
    (ErrorCategory.TRANSPORT, "transport failure"),
    (ErrorCategory.INCOMPLETE, "incomplete response"),
):
    adapters = {
        "luna": ScriptedAdapter("luna", [err(category)]),
        "haiku": ScriptedAdapter("haiku", [None]),
        "qwen": ScriptedAdapter("qwen", [None]),
    }
    router, _ = router_with(adapters)
    result = router.run(req())
    c.ok(result.provider == "haiku", "Luna %s falls back to Haiku" % label)
    c.ok(adapters["qwen"].calls == 0, "Luna %s does not skip past Haiku to Qwen" % label)

adapters = {
    "luna": ScriptedAdapter("luna", [err(ErrorCategory.OUTAGE, "luna")]),
    "haiku": ScriptedAdapter("haiku", [err(ErrorCategory.RATE_LIMIT, "haiku")]),
    "qwen": ScriptedAdapter("qwen", [None]),
}
router, _ = router_with(adapters)
c.ok(router.run(req()).provider == "qwen", "Luna and Haiku both failing reaches Qwen")

adapters = {
    "luna": ScriptedAdapter("luna", [err(ErrorCategory.OUTAGE, "luna")]),
    "haiku": ScriptedAdapter("haiku", [err(ErrorCategory.OUTAGE, "haiku")]),
    "qwen": ScriptedAdapter("qwen", [err(ErrorCategory.TRANSPORT, "qwen")]),
}
router, _ = router_with(adapters)
c.raises(lambda: router.run(req()), ProviderError, "all three failing raises the last error")
c.ok(all(a.calls == 1 for a in adapters.values()),
     "each provider is attempted exactly once when the failure is not retryable")

adapters = {
    "luna": ScriptedAdapter("luna", [err(ErrorCategory.TRANSPORT, "luna")]),
    "haiku": ScriptedAdapter("haiku", [err(ErrorCategory.TRANSPORT, "haiku")]),
    "qwen": ScriptedAdapter("qwen", [err(ErrorCategory.TRANSPORT, "qwen")]),
}
router, _ = router_with(adapters)
try:
    router.run(req())
except ProviderError:
    pass
c.ok(sum(a.calls for a in adapters.values()) == 3,
     "a full failure costs exactly one call per provider — no loop")

# Configurable order, no code change.
adapters = {p: ScriptedAdapter(p, [None]) for p in PROVIDER_IDS}
router, _ = router_with(adapters, llm_fallback_order="qwen,haiku")
c.ok(router.provider_chain() == ["qwen", "haiku"], "fallback order is configuration-driven")
c.ok(router.run(req()).provider == "qwen", "a reordered chain calls the new first provider")


# ── failures that must NOT fall back ────────────────────────────────────────

for category, label in (
    (ErrorCategory.REFUSAL, "a safety refusal"),
    (ErrorCategory.BAD_REQUEST, "a malformed request (our bug)"),
):
    adapters = {
        "luna": ScriptedAdapter("luna", [err(category)]),
        "haiku": ScriptedAdapter("haiku", [None]),
        "qwen": ScriptedAdapter("qwen", [None]),
    }
    router, _ = router_with(adapters)
    c.raises(lambda r=router: r.run(req()), ProviderError, "%s stops the chain" % label)
    c.ok(adapters["haiku"].calls == 0 and adapters["qwen"].calls == 0,
         "%s never reaches another provider" % label)

c.ok(ErrorCategory.REFUSAL not in ELIGIBLE_FOR_FALLBACK,
     "REFUSAL is not in the fallback allow-list")
c.ok(ErrorCategory.BAD_REQUEST not in ELIGIBLE_FOR_FALLBACK,
     "BAD_REQUEST is not in the fallback allow-list")

# Nibbler's own auth is enforced in the routers, before any model is built.
# The proof that it cannot reach a provider is that no LLM code runs first:
# /connect/chat calls _require_premium and _get_item before LLMService exists.
import inspect  # noqa: E402
from app.routers import connect as connect_router  # noqa: E402
chat_src = inspect.getsource(connect_router.chat)
c.ok(chat_src.index("_require_premium") < chat_src.index("LLMService"),
     "Nibbler auth/ownership checks run before any provider is constructed")


# ── circuit breaker ─────────────────────────────────────────────────────────

breaker = CircuitBreaker(cooldown_seconds=60)
c.ok(not breaker.is_open("luna"), "a fresh breaker is closed")
breaker.record_failure("luna", err(ErrorCategory.INSUFFICIENT_CREDIT))
c.ok(breaker.is_open("luna"), "insufficient credit opens the breaker")
c.ok(not breaker.is_open("haiku"), "the breaker is per-provider, not global")
c.ok(0 < breaker.seconds_remaining("luna") <= 60, "the cooldown is bounded")
breaker.record_success("luna")
c.ok(not breaker.is_open("luna"), "a success closes the breaker")

breaker.record_failure("luna", err(ErrorCategory.SCHEMA))
c.ok(not breaker.is_open("luna"), "bad output does not open the breaker (the provider is fine)")

expired = CircuitBreaker(cooldown_seconds=1)
expired.record_failure("qwen", err(ErrorCategory.TRANSPORT, "qwen"))
expired._open_until["qwen"] = 0.0  # simulate the cooldown having elapsed
c.ok(not expired.is_open("qwen"), "the breaker closes itself once the cooldown expires")

adapters = {
    "luna": ScriptedAdapter("luna", [None]),
    "haiku": ScriptedAdapter("haiku", [None]),
    "qwen": ScriptedAdapter("qwen", [None]),
}
breaker = CircuitBreaker(60)
breaker.record_failure("luna", err(ErrorCategory.PROVIDER_AUTH))
router = LLMRouter(make_settings(), adapters=adapters, breaker=breaker)
result = router.run(req())
c.ok(result.provider == "haiku" and adapters["luna"].calls == 0,
     "an open breaker skips the provider without calling it")

c.ok("PROCESS-LOCAL" in (sys.modules["app.services.llm.circuit"].__doc__ or ""),
     "the circuit breaker documents that it does not coordinate across replicas")


# ── status-code classification ──────────────────────────────────────────────

cases = [
    (429, "rate limit exceeded", ErrorCategory.RATE_LIMIT),
    (429, "You exceeded your current quota, please check your billing", ErrorCategory.QUOTA),
    (401, "invalid api key", ErrorCategory.PROVIDER_AUTH),
    (403, "credit balance is too low", ErrorCategory.BILLING),
    (402, "payment required", ErrorCategory.INSUFFICIENT_CREDIT),
    (400, "invalid parameter: max_tokens", ErrorCategory.BAD_REQUEST),
    (400, "insufficient_quota", ErrorCategory.INSUFFICIENT_CREDIT),
    (500, "internal error", ErrorCategory.OUTAGE),
    (503, "overloaded", ErrorCategory.OUTAGE),
    (408, "timeout", ErrorCategory.TIMEOUT),
]
for status, message, expected in cases:
    got = classify_status(status, message, "luna")
    c.ok(got == expected, "HTTP %s %r → %s" % (status, message[:28], expected))

c.ok(classify_status(401, "bad key", "luna") != classify_status(403, "credit balance", "luna"),
     "provider auth failure and billing failure are different categories")

sys.exit(1 if c.finish() else 0)
