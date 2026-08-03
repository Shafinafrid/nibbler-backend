"""
Regression tests for the twelve findings in Hermes's 2026-08-02 audit.

Each section names the finding it locks down. These are the defects that the
first round of tests did not catch, mostly because they live in the seam
between the code and its runtime: an object's lifetime, an SDK's default, a
category that was eligible when it should not have been. Passing unit tests
said nothing about any of them.

    .venv/bin/python tests/test_llm_hardening.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermetic  # noqa: E402,F401

from llm_fakes import (  # noqa: E402
    Checks, FakeHaikuClient, FakeLunaClient, FakeQwenClient,
    haiku_response, luna_response, make_settings, qwen_response, valid_deck,
)

from types import SimpleNamespace  # noqa: E402

from app.services.llm import LLMService  # noqa: E402
from app.services.llm.base import LLMRequest  # noqa: E402
from app.services.llm.errors import (  # noqa: E402
    ELIGIBLE_FOR_FALLBACK, ErrorCategory, ProviderError,
)
from app.services.llm.haiku import HaikuAdapter  # noqa: E402
from app.services.llm.jsonschema_lite import schema_errors  # noqa: E402
from app.services.llm.luna import LunaAdapter  # noqa: E402
from app.services.llm.qwen import QwenAdapter  # noqa: E402
from app.services.llm.router import (  # noqa: E402
    LLMConfigError, LLMRouter, QWEN_MIN_CONTEXT_TOKENS, get_shared_breaker,
    reset_shared_breaker, validate_llm_settings,
)
from app.services.llm.schemas import wisdom_schema  # noqa: E402
from app.services.llm.usage import Usage  # noqa: E402
from app.services.llm.validation import enforce_schema, validate_wisdom  # noqa: E402

c = Checks("LLM hardening (Hermes findings)")

DECK = valid_deck(5, 4)


def deck():
    """A FRESH deck for every fake response.

    `LLMService` finishes a Wisdom deck in place — it strips the schema's null
    placeholders and shuffles quiz options on the object the adapter returned.
    In production that object is a freshly parsed response, so mutating it is
    free. In a test, handing the same dict to two fake responses means the
    second one is whatever the first run left behind, which fails schema
    validation for reasons that have nothing to do with the test.
    """
    return json.loads(json.dumps(DECK))


def req(schema=None):
    return LLMRequest(
        operation="wisdom_session", system="SYS",
        messages=[{"role": "user", "content": "hi"}], max_visible_tokens=1000,
        json_schema=schema, schema_name="wisdom_session" if schema else None,
    )


# ══ Finding 1 — circuit breaker died with each request ══════════════════════
# LLMService() is constructed per request in three places. A breaker owned by
# the router instance therefore forgot everything before the next request.

reset_shared_breaker()
settings = make_settings()
a = LLMService(settings=settings)
b = LLMService(settings=settings)
c.ok(a.router.breaker is b.router.breaker,
     "two LLMService instances share one breaker (it must outlive the request)")
c.ok(a.router.breaker is get_shared_breaker(),
     "the shared breaker is the module-level one, not a per-router copy")

reset_shared_breaker()
first = LLMService(settings=settings, router=LLMRouter(settings, adapters={
    "luna": LunaAdapter(settings, client=FakeLunaClient(
        errors=[__import__("tests.llm_fakes", fromlist=["x"]).openai_status_error(401)])),
    "haiku": HaikuAdapter(settings, client=FakeHaikuClient([haiku_response(tool_input=deck())])),
}))
first.generate_wisdom_session("B", None, {}, ["x"], 5, 5)

luna_spy = FakeLunaClient([luna_response(json.dumps(deck()))])
second = LLMService(settings=settings, router=LLMRouter(settings, adapters={
    "luna": LunaAdapter(settings, client=luna_spy),
    "haiku": HaikuAdapter(settings, client=FakeHaikuClient([haiku_response(tool_input=deck())])),
}))
second.generate_wisdom_session("B", None, {}, ["x"], 5, 5)
c.ok(not luna_spy.calls,
     "a breaker opened by ONE request still skips Luna on the NEXT request")
reset_shared_breaker()


# ══ Finding 2 — hidden SDK retries multiplied billed calls ══════════════════
# Both SDKs default to max_retries=2, so one logged "attempt" could be three
# billed HTTP calls and a three-provider chain up to seven.

luna_client = LunaAdapter(make_settings())._get_client()
haiku_client = HaikuAdapter(make_settings())._get_client()
qwen_client = QwenAdapter(make_settings())._get_client()
c.ok(luna_client.max_retries == 0, "the Luna client performs no silent SDK retries")
c.ok(haiku_client.max_retries == 0, "the Haiku client performs no silent SDK retries")
c.ok(qwen_client.max_retries == 0, "the Qwen client performs no silent SDK retries")


# ══ Finding 3 — our own bugs could buy paid fallback ════════════════════════

c.ok(ErrorCategory.UNKNOWN not in ELIGIBLE_FOR_FALLBACK,
     "an unclassified failure does not spend money at the next provider")
c.ok(ErrorCategory.INTERNAL not in ELIGIBLE_FOR_FALLBACK,
     "an adapter bug does not spend money at the next provider")


class ExplodingAdapter:
    name = "luna"
    model = "gpt-5.6-luna"
    reasoning = "low"

    def is_configured(self):
        return True

    def generate(self, request):
        raise TypeError("a bug in our own code")


haiku_spy = FakeHaikuClient([haiku_response(tool_input=deck())])
reset_shared_breaker()
router = LLMRouter(make_settings(), adapters={
    "luna": ExplodingAdapter(),
    "haiku": HaikuAdapter(make_settings(), client=haiku_spy),
})
c.raises(lambda: router.run(req()), ProviderError,
         "an adapter TypeError surfaces as an error", category=ErrorCategory.INTERNAL)
c.ok(not haiku_spy.calls,
     "a deterministic bug in our code is not rediscovered at a second provider's expense")


# ══ Finding 4 — Qwen refusals were mistaken for empty responses ═════════════

def qwen_with(response):
    return QwenAdapter(make_settings(), client=FakeQwenClient([response]))


refusal_msg = SimpleNamespace(
    choices=[SimpleNamespace(finish_reason="stop",
                             message=SimpleNamespace(content=None,
                                                     refusal="I can't help with that"))],
    usage=SimpleNamespace(prompt_tokens=800, completion_tokens=20),
)
c.raises(lambda: qwen_with(refusal_msg).generate(req()), ProviderError,
         "a Qwen message.refusal is a REFUSAL, not an empty response",
         category=ErrorCategory.REFUSAL)

for reason in ("content_filter", "refusal"):
    filtered = qwen_response("", finish_reason=reason)
    c.raises(lambda f=filtered: qwen_with(f).generate(req()), ProviderError,
             "a Qwen finish_reason=%s is a REFUSAL" % reason,
             category=ErrorCategory.REFUSAL)

# The consequence that made the misclassification expensive: an "empty"
# response is retried and then handed on; a refusal must do neither.
reset_shared_breaker()
qwen_client = FakeQwenClient([refusal_msg, qwen_response(json.dumps(deck()))])
haiku_spy = FakeHaikuClient([haiku_response(tool_input=deck())])
router = LLMRouter(make_settings(llm_fallback_order="qwen,haiku"), adapters={
    "qwen": QwenAdapter(make_settings(), client=qwen_client),
    "haiku": HaikuAdapter(make_settings(), client=haiku_spy),
})
c.raises(lambda: router.run(req()), ProviderError, "a Qwen refusal stops the chain")
c.ok(len(qwen_client.calls) == 1, "a refusal is not retried at the same provider")
c.ok(not haiku_spy.calls, "a refusal is not re-asked of the next provider")


# ══ Finding 5 — billed failures were recorded as free ═══════════════════════
# Refusals, truncations and empty completions all generate billed tokens.

cases = [
    ("a refusal", luna_response("", refusal=True), ErrorCategory.REFUSAL),
    ("a truncated answer", luna_response('{"ti', status="incomplete",
                                         incomplete_reason="max_output_tokens"),
     ErrorCategory.INCOMPLETE),
    ("an empty completion", luna_response("   "), ErrorCategory.EMPTY_RESPONSE),
    ("unparseable output", luna_response("sorry, no JSON here"), ErrorCategory.SCHEMA),
]
for label, response, expected in cases:
    adapter = LunaAdapter(make_settings(), client=FakeLunaClient([response]))
    try:
        adapter.generate(req(wisdom_schema(5, 4)))
        c.ok(False, "%s should have raised" % label)
    except ProviderError as e:
        c.ok(e.category == expected, "%s is categorised as %s" % (label, expected))
        c.ok(e.usage is not None and e.usage.output_tokens > 0,
             "%s still reports the tokens it burned" % label)
        c.ok(e.model == "gpt-5.6-luna", "%s reports which model burned them" % label)

reset_shared_breaker()
import logging  # noqa: E402


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
log = logging.getLogger("app.services.llm")
log.addHandler(cap)
log.setLevel(logging.INFO)

router = LLMRouter(make_settings(llm_routing_mode="single", llm_active_provider="luna"),
                   adapters={"luna": LunaAdapter(make_settings(), client=FakeLunaClient(
                       [luna_response("", refusal=True)]))})
try:
    router.run(req(wisdom_schema(5, 4)))
except ProviderError:
    pass
events = [json.loads(l.split(" ", 1)[1]) for l in cap.lines if l.startswith("llm_")]
attempt = next(e for e in events if e.get("success") is False)
summary = next(e for e in events if "total_cost_usd" in e)
c.ok(attempt.get("estimated_cost_usd", 0) > 0,
     "a refused attempt is logged with a non-zero cost")
c.ok(summary["total_cost_usd"] > 0,
     "the request total includes a failure that produced no usable output")
log.removeHandler(cap)
reset_shared_breaker()


# ══ Finding 6 — structured output was never checked locally ═════════════════

schema = wisdom_schema(5, 4)
c.ok(schema_errors(DECK, schema) == [], "a good deck passes the local schema check")

missing = json.loads(json.dumps(DECK))
del missing["headline"]
c.ok(any("headline" in e for e in schema_errors(missing, schema)),
     "a missing required key is caught locally")

wrong_type = json.loads(json.dumps(DECK))
wrong_type["cards"] = "not a list"
c.ok(schema_errors(wrong_type, schema), "a wrong type is caught locally")

extra = json.loads(json.dumps(DECK))
extra["surprise"] = 1
c.ok(any("surprise" in e for e in schema_errors(extra, schema)),
     "an unexpected key is caught locally (additionalProperties: false)")

short = json.loads(json.dumps(DECK))
short["cards"] = short["cards"][:3]
c.ok(any("at least" in e for e in schema_errors(short, schema)),
     "minItems is enforced locally, not just requested of the provider")

bad_enum = json.loads(json.dumps(DECK))
bad_enum["cards"][1]["kind"] = "sidebar"
c.ok(any("not one of" in e for e in schema_errors(bad_enum, schema)),
     "an out-of-enum card kind is caught locally")

c.ok(schema_errors(None, {"type": ["string", "null"]}) == [],
     "a nullable field accepts null")
c.ok(schema_errors(True, {"type": "integer"}), "a boolean is not accepted as an integer")

c.raises(lambda: enforce_schema(missing, schema, "qwen"), ProviderError,
         "enforce_schema raises a retryable SCHEMA failure", category=ErrorCategory.SCHEMA)

# The path that made this necessary: a provider answering in prose bypasses
# structured output entirely, and Haiku treats the schema as a hint.
bypassed = HaikuAdapter(make_settings(), client=FakeHaikuClient(
    [haiku_response(text=json.dumps(missing))]))
svc = LLMService(settings=make_settings(llm_routing_mode="single",
                                        llm_active_provider="haiku"),
                 router=LLMRouter(make_settings(llm_routing_mode="single",
                                                llm_active_provider="haiku"),
                                  adapters={"haiku": bypassed}))
c.raises(lambda: svc.generate_wisdom_session("B", None, {}, ["x"], 5, 5), ProviderError,
         "a schema-violating response is rejected even when the tool was bypassed")
reset_shared_breaker()


# ══ Finding 7 — pull-quotes were never checked against the book ═════════════

SOURCE = ["Compound interest is the eighth wonder of the world. "
          "He who understands it, earns it; he who doesn't, pays it."]


def deck_with_highlight(quote):
    d = json.loads(json.dumps(DECK))
    d["cards"][1]["highlight"] = quote
    return d


grounded = deck_with_highlight("Compound interest is the eighth wonder of the world.")
c.ok(validate_wisdom(grounded, provider="luna", card_target=5, quiz_target=4,
                     interaction_kind="prompt", source_chunks=SOURCE) is grounded,
     "a quote that really appears in the source passes")

invented = deck_with_highlight("Money is merely a story we agree to believe together.")
c.raises(lambda: validate_wisdom(invented, provider="luna", card_target=5, quiz_target=4,
                                 interaction_kind="prompt", source_chunks=SOURCE),
         ProviderError, "a fabricated pull-quote is rejected")

# Faithful quoting that normalises punctuation or re-wraps is not fabrication.
reflowed = deck_with_highlight("Compound  interest is the eighth\nwonder of the world.")
c.ok(validate_wisdom(reflowed, provider="luna", card_target=5, quiz_target=4,
                     interaction_kind="prompt", source_chunks=SOURCE) is reflowed,
     "re-wrapped whitespace in a real quote is not treated as fabrication")

curly = deck_with_highlight("He who understands it, earns it; he who doesn’t, pays it.")
c.ok(validate_wisdom(curly, provider="luna", card_target=5, quiz_target=4,
                     interaction_kind="prompt", source_chunks=SOURCE) is curly,
     "a smart apostrophe in a real quote is not treated as fabrication")

tiny = deck_with_highlight("earns it")
c.ok(validate_wisdom(tiny, provider="luna", card_target=5, quiz_target=4,
                     interaction_kind="prompt", source_chunks=SOURCE) is tiny,
     "a fragment too short to be evidence is not judged")

no_source = deck_with_highlight("Anything at all, unverifiable.")
c.ok(validate_wisdom(no_source, provider="luna", card_target=5, quiz_target=4,
                     interaction_kind="prompt") is no_source,
     "without source chunks the grounding check is skipped, not guessed")


# ══ Finding 8 — an 8K Qwen context cannot serve a 15-minute deck ════════════

c.ok(make_settings().qwen_context_size >= QWEN_MIN_CONTEXT_TOKENS,
     "the default declared Qwen context is large enough")
c.raises(lambda: validate_llm_settings(make_settings(qwen_context_size=8192)),
         LLMConfigError, "an 8K declared Qwen context is rejected at startup")
c.ok(QWEN_MIN_CONTEXT_TOKENS >= 16384,
     "the minimum covers ~8.7K input plus the 7,980-token 15-minute output budget")

script = open(os.path.join(hermetic.BACKEND, "scripts", "run_qwen_local.sh")).read()
c.ok("QWEN_CONTEXT_SIZE:-32768" in script,
     "the launch script defaults llama-server to a 32K window")


# ══ Finding 9 — configuration validation was too permissive ═════════════════

c.raises(lambda: validate_llm_settings(make_settings(
    qwen_base_url="http://qwen.example.com/v1")),
    LLMConfigError, "a public plain-HTTP Qwen endpoint is rejected")
try:
    validate_llm_settings(make_settings(qwen_base_url="http://127.0.0.1:8080/v1"))
    c.ok(True, "plain HTTP to your own machine is still allowed")
except LLMConfigError:
    c.ok(False, "plain HTTP to your own machine is still allowed")

for bad in ("claude-haiku-3", "claude-haiku-3-5", "claude-3-haiku-20240307"):
    c.raises(lambda m=bad: validate_llm_settings(make_settings(anthropic_llm_model=m)),
             LLMConfigError, "an older Haiku model (%s) is rejected" % bad)
for bad in ("gpt-5.6-luna-preview", "gpt-5.6-luna-2026-07-09-experimental"):
    c.raises(lambda m=bad: validate_llm_settings(make_settings(openai_llm_model=m)),
             LLMConfigError, "an unapproved Luna variant (%s) is rejected" % bad)
c.raises(lambda: validate_llm_settings(make_settings(openai_llm_model="gpt-5.6")),
         LLMConfigError, "the Sol-routing alias is still rejected")


# ══ Finding 10 — tests could read the real .env ═════════════════════════════

c.ok(hermetic.env_file_is_unreachable(),
     "no .env is visible from the test working directory")
c.ok(os.getcwd() != hermetic.BACKEND,
     "tests do not run from the repo root, where .env would resolve")
real_env = os.path.join(hermetic.BACKEND, ".env")
if os.path.exists(real_env):
    from app.config import Settings
    c.ok(Settings(_env_file=None, database_url="sqlite:///x",
                  firebase_project_id="t").aws_secret_access_key == "",
         "a real .env exists on this machine but supplies nothing to the tests")
else:
    c.ok(True, "no .env on this machine — nothing to leak")


# ══ Finding 12 — image generation was off only by accident ══════════════════

c.ok(make_settings().image_generation_enabled is False,
     "image generation defaults to OFF, not merely unreachable")
c.ok(make_settings(openai_llm_api_key="sk-luna").image_generation_enabled is False,
     "configuring Luna does not switch image generation on")


# ══════════════════════════════════════════════════════════════════════════
# RE-AUDIT 2026-08-02 — four findings the first repair pass did not close
# ══════════════════════════════════════════════════════════════════════════

import subprocess  # noqa: E402

# ── Re-audit 1 — hermetic used setdefault, so exported vars survived ────────
# Proved in a subprocess with a deliberately poisoned environment: asserting
# against this process cannot work, because hermetic already ran here.

poisoned = dict(os.environ)
poisoned.update(
    DATABASE_URL="postgresql://real:secret@prod.example.com:5432/nibbler",
    AWS_ACCESS_KEY_ID="AKIAPRETENDPRODUCTION",
    AWS_SECRET_ACCESS_KEY="pretend-production-secret",
    ANTHROPIC_LLM_API_KEY="sk-ant-pretend-production",
    PINECONE_API_KEY="pretend-production-pinecone",
    VOYAGE_API_KEY="pretend-production-voyage",
)
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r); import hermetic, os, json;"
     "print(json.dumps({"
     "'db': os.environ['DATABASE_URL'],"
     "'aws': os.environ['AWS_ACCESS_KEY_ID'],"
     "'anthropic': os.environ['ANTHROPIC_LLM_API_KEY'],"
     "'pinecone': os.environ['PINECONE_API_KEY'],"
     "'voyage': os.environ['VOYAGE_API_KEY'],"
     "'purged': hermetic.production_env_is_purged(),"
     "'env_file': hermetic.env_file_is_unreachable()}))"
     % os.path.join(hermetic.BACKEND, "tests")],
    capture_output=True, text=True, env=poisoned, cwd=hermetic.BACKEND,
)
c.ok(probe.returncode == 0, "the poisoned-environment probe ran (%s)" % probe.stderr[-200:])
if probe.returncode == 0:
    seen = json.loads(probe.stdout.strip().splitlines()[-1])
    c.ok(seen["aws"] == "", "an exported AWS key is OVERWRITTEN, not merely defaulted")
    c.ok(seen["anthropic"] == "", "an exported Anthropic key is overwritten")
    c.ok(seen["pinecone"] == "" and seen["voyage"] == "",
         "exported Pinecone and Voyage keys are overwritten")
    c.ok(seen["db"].startswith("sqlite://"),
         "an inherited production Postgres URL is replaced with a sandbox SQLite file")
    c.ok(seen["purged"] is True, "no credential-shaped variable survives import")
    c.ok(seen["env_file"] is True, "the real .env is unreachable even from the repo root")


# ── Re-audit 2 — a blocked S3 call was hidden inside a green run ────────────
# The guard must fail the RUN, not just the request: boto3 swallows the error,
# the endpoint 502s, and a test that accepts a 502 passes over a real
# connection attempt to AWS.

guard = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r); import hermetic, socket;\n"
     "try:\n"
     "    socket.create_connection(('93.184.216.34', 443), timeout=1)\n"
     "except Exception as e:\n"
     "    print('blocked:', type(e).__name__)\n"
     "print('suite finished happily')\n"
     % os.path.join(hermetic.BACKEND, "tests")],
    capture_output=True, text=True, cwd=hermetic.BACKEND,
)
c.ok("blocked: NetworkBlocked" in guard.stdout,
     "an outbound connection is refused rather than dialled")
c.ok("suite finished happily" in guard.stdout,
     "the script itself continued — exactly the case that used to hide the call")
c.ok(guard.returncode != 0,
     "but the RUN still exits non-zero, so a swallowed network call cannot pass")
c.ok("NETWORK_ATTEMPT_BLOCKED" in guard.stderr,
     "the blocked address is reported")

loopback = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, %r); import hermetic, socket;\n"
     "try:\n"
     "    socket.create_connection(('127.0.0.1', 1), timeout=1)\n"
     "except OSError as e:\n"
     "    print('reached the OS, not the guard:', e.__class__.__name__)\n"
     % os.path.join(hermetic.BACKEND, "tests")],
    capture_output=True, text=True, cwd=hermetic.BACKEND,
)
c.ok(loopback.returncode == 0 and "reached the OS" in loopback.stdout,
     "loopback is left open — the guard blocks the outside world, not localhost")


# ── Re-audit 3 — telemetry gaps ────────────────────────────────────────────

def events_from(run):
    cap = Capture()
    lg = logging.getLogger("app.services.llm")
    lg.addHandler(cap)
    lg.setLevel(logging.INFO)
    try:
        run()
    except ProviderError:
        pass
    finally:
        lg.removeHandler(cap)
    return [json.loads(l.split(" ", 1)[1]) for l in cap.lines if l.startswith("llm_")]


reset_shared_breaker()
s = make_settings(llm_routing_mode="single", llm_active_provider="luna")
retry_router = LLMRouter(s, adapters={"luna": LunaAdapter(s, client=FakeLunaClient([
    luna_response("not json"), luna_response(json.dumps(deck()))]))})
svc = LLMService(settings=s, router=retry_router)
evs = events_from(lambda: svc.generate_wisdom_session("B", None, {}, ["x"], 5, 5))
summary = next(e for e in evs if "total_cost_usd" in e)

ids = {e.get("request_id") for e in evs}
c.ok(len(ids) == 1 and None not in ids,
     "every event of one logical request carries the same request_id")
c.ok(all(len(e.get("request_id") or "") > 0 for e in evs),
     "no event is emitted without a correlation id")
c.ok(summary["attempts"] == 2 and summary["providers_tried"] == 1,
     "a same-provider retry is two attempts at ONE provider")
c.ok(summary["fell_back"] is False,
     "a same-provider retry does NOT report fell_back=true")

reset_shared_breaker()
s2 = make_settings()
fell = LLMService(settings=s2, router=LLMRouter(s2, adapters={
    "luna": LunaAdapter(s2, client=FakeLunaClient(
        errors=[__import__("tests.llm_fakes", fromlist=["x"]).openai_status_error(503)])),
    "haiku": HaikuAdapter(s2, client=FakeHaikuClient([haiku_response(tool_input=deck())])),
}))
evs = events_from(lambda: fell.generate_wisdom_session("B", None, {}, ["x"], 5, 5))
summary = next(e for e in evs if "total_cost_usd" in e)
c.ok(summary["providers_tried"] == 2 and summary["fell_back"] is True,
     "a real cross-provider fallback DOES report fell_back=true")
c.ok(len({e.get("request_id") for e in evs}) == 1,
     "a fallback's attempts share one request_id")

# Per-attempt latency: a translated SDK error has no latency of its own, so the
# router must supply this attempt's duration — not time since the request began.
slow_attempts = [e for e in evs if e.get("success") is False]
c.ok(all(e["latency_ms"] < 1000 for e in slow_attempts),
     "a failed attempt reports its own duration, not cumulative request time")
c.ok(all("latency_ms" in e for e in evs if "attempt" in e),
     "every attempt event carries a latency")
reset_shared_breaker()


# ── Re-audit 4 — Qwen and model validation still too loose ─────────────────

from app.services.llm.router import (  # noqa: E402
    _QWEN_REQUIRED_TOKENS, QWEN_SAFETY_MARGIN,
)

c.ok(QWEN_MIN_CONTEXT_TOKENS >= _QWEN_REQUIRED_TOKENS,
     "the minimum context is at least the code's own derived requirement (%d)"
     % _QWEN_REQUIRED_TOKENS)
c.ok(QWEN_MIN_CONTEXT_TOKENS >= _QWEN_REQUIRED_TOKENS * QWEN_SAFETY_MARGIN * 0.99,
     "the minimum carries a real safety margin above the requirement")
c.raises(lambda: validate_llm_settings(make_settings(qwen_context_size=16384)),
         LLMConfigError, "16,384 is now rejected — it sat below the requirement")
c.ok(make_settings().qwen_context_size >= QWEN_MIN_CONTEXT_TOKENS,
     "the shipped default still satisfies the raised minimum")

c.raises(lambda: validate_llm_settings(make_settings(
    app_env="production", qwen_base_url="http://127.0.0.1:8080/v1")),
    LLMConfigError, "a loopback Qwen URL in PRODUCTION is an error, not a warning")
c.raises(lambda: validate_llm_settings(make_settings(
    app_env="production", qwen_base_url="http://192.168.1.9:8080/v1")),
    LLMConfigError, "a private-LAN Qwen URL in production is an error")
try:
    warns = validate_llm_settings(make_settings(
        app_env="development", qwen_base_url="http://127.0.0.1:8080/v1"))
    c.ok(any("local development" in w for w in warns),
         "the same URL outside production is still just a warning")
except LLMConfigError:
    c.ok(False, "local development against a local server must remain allowed")

# Runtime checks must be no weaker than startup checks — a deployment that
# somehow skips validation must still be unable to call an unapproved model.
for bad in ("gpt-5.6-luna-preview", "gpt-5.6", "gpt-5.6-luna-2026-experimental"):
    adapter = LunaAdapter(make_settings(openai_llm_model=bad),
                          client=FakeLunaClient([luna_response(json.dumps(deck()))]))
    c.raises(lambda a=adapter: a.generate(req()), ProviderError,
             "the Luna ADAPTER refuses %s at call time" % bad,
             category=ErrorCategory.NOT_CONFIGURED)

for bad in ("claude-haiku-3", "claude-haiku-3-5", "claude-3-haiku-20240307",
            "claude-sonnet-4-6"):
    adapter = HaikuAdapter(make_settings(anthropic_llm_model=bad),
                           client=FakeHaikuClient([haiku_response(tool_input=deck())]))
    c.raises(lambda a=adapter: a.generate(req()), ProviderError,
             "the Haiku ADAPTER refuses %s at call time" % bad,
             category=ErrorCategory.NOT_CONFIGURED)

from app.services.llm.haiku import ALLOWED_HAIKU_MODELS  # noqa: E402
from app.services.llm.luna import ALLOWED_LUNA_MODELS  # noqa: E402
from app.services.llm import router as router_mod  # noqa: E402
c.ok(router_mod.ALLOWED_LUNA_MODELS is ALLOWED_LUNA_MODELS
     and router_mod.ALLOWED_HAIKU_MODELS is ALLOWED_HAIKU_MODELS,
     "startup and runtime share ONE allow-list, so they cannot drift apart")

sys.exit(1 if c.finish() else 0)
