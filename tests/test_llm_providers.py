"""
Adapter-level behaviour: what each provider is actually SENT, and how its
reply is read back.

Routing is tested elsewhere. What matters here is the wire detail that a
routing test would happily paper over — that Luna is asked for low reasoning in
standard mode with enough token headroom for a hidden reasoning pass, that
Haiku's cache marker sits on the stable block and not the per-request one, that
Qwen is told not to think, and that each SDK's usage object is flattened
correctly. Getting any of these silently wrong costs money rather than failing.

No network, no credentials: every adapter is handed a fake client.

    .venv/bin/python tests/test_llm_providers.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_fakes import (  # noqa: E402
    Checks, FakeHaikuClient, FakeLunaClient, FakeQwenClient,
    anthropic_status_error, haiku_response, luna_response, make_settings,
    openai_connection_error, openai_status_error, openai_timeout, qwen_response,
    valid_deck,
)

from app.services.llm.base import LLMRequest  # noqa: E402
from app.services.llm.errors import ErrorCategory, ProviderError  # noqa: E402
from app.services.llm.haiku import HaikuAdapter  # noqa: E402
from app.services.llm.luna import (  # noqa: E402
    ALLOWED_REASONING_EFFORTS, LunaAdapter, _total_output_budget,
)
from app.services.llm.qwen import NO_THINK, QwenAdapter, is_local_url  # noqa: E402
from app.services.llm.schemas import wisdom_schema  # noqa: E402
from app.services.llm.usage import PRICING, Usage, estimate_cost  # noqa: E402

c = Checks("LLM providers")

DECK = valid_deck()
DECK_JSON = json.dumps(DECK)


def json_request(max_visible=1000):
    return LLMRequest(
        operation="wisdom_session", system="STABLE SYSTEM PROMPT",
        messages=[{"role": "user", "content": "excerpts here"}],
        max_visible_tokens=max_visible,
        json_schema=wisdom_schema(5, 4), schema_name="wisdom_session",
    )


def text_request():
    return LLMRequest(
        operation="connect_chat", system="STABLE SYSTEM PROMPT",
        context="THE BOOK: ...", messages=[{"role": "user", "content": "what does it say?"}],
        max_visible_tokens=600,
    )


# ══ Luna ════════════════════════════════════════════════════════════════════

client = FakeLunaClient([luna_response(DECK_JSON)])
adapter = LunaAdapter(make_settings(), client=client)
result = adapter.generate(json_request())
sent = client.calls[0]

c.ok(sent["model"] == "gpt-5.6-luna", "Luna is called with the explicit gpt-5.6-luna id")
c.ok(sent["reasoning"]["effort"] == "low", "Luna is asked for low reasoning effort")
c.ok(sent["reasoning"]["mode"] == "standard",
     "Luna reasoning mode is pinned to standard (pro is a separate, pricier path)")
c.ok(sent["text"]["format"]["type"] == "json_schema" and sent["text"]["format"]["strict"] is True,
     "Luna JSON workflows use strict Structured Outputs")
c.ok(sent["instructions"] == "STABLE SYSTEM PROMPT",
     "the cacheable system text goes in instructions, separate from the input")
c.ok(result.data == DECK, "Luna's structured output is returned as parsed data")

c.ok(sent["max_output_tokens"] > 1000,
     "Luna's output budget exceeds the visible size — hidden reasoning needs headroom")
c.ok(_total_output_budget(4230) >= 8460 and _total_output_budget(400) >= 2400,
     "the headroom scales with the answer and has a floor for small answers")
c.ok(_total_output_budget(7980) < 128000,
     "the budget is never simply the provider maximum")

usage = result.usage
c.ok(usage.input_tokens == 1000 and usage.reasoning_tokens == 100,
     "Luna usage is normalized (input + hidden reasoning tokens)")
c.ok(usage.output_tokens == 500 and usage.visible_output_tokens == 400,
     "total output includes reasoning; visible output excludes it")

cached = LunaAdapter(make_settings(), client=FakeLunaClient(
    [luna_response(DECK_JSON, input_tokens=5000, cached=4000)]))
c.ok(cached.generate(json_request()).usage.cached_input_tokens == 4000,
     "Luna cached-input tokens are captured")

# Reasoning above the cap cannot be reached even by bypassing startup checks.
for effort in ("medium", "high", "xhigh", "max"):
    bad = LunaAdapter(make_settings(openai_llm_reasoning_effort=effort),
                      client=FakeLunaClient([luna_response(DECK_JSON)]))
    c.raises(lambda a=bad: a.generate(json_request()), ProviderError,
             "Luna refuses to run at %r reasoning" % effort,
             category=ErrorCategory.NOT_CONFIGURED)
c.ok(set(ALLOWED_REASONING_EFFORTS) == {"none", "minimal", "low"},
     "only none/minimal/low are permitted efforts")

alias = LunaAdapter(make_settings(openai_llm_model="gpt-5.6"),
                    client=FakeLunaClient([luna_response(DECK_JSON)]))
c.raises(lambda: alias.generate(json_request()), ProviderError,
         "the bare gpt-5.6 alias is refused at call time too",
         category=ErrorCategory.NOT_CONFIGURED)

refusing = LunaAdapter(make_settings(), client=FakeLunaClient([luna_response("", refusal=True)]))
c.raises(lambda: refusing.generate(json_request()), ProviderError,
         "a Luna refusal is detected", category=ErrorCategory.REFUSAL)

truncated = LunaAdapter(make_settings(), client=FakeLunaClient(
    [luna_response('{"title": "half a de', status="incomplete",
                   incomplete_reason="max_output_tokens")]))
c.raises(lambda: truncated.generate(json_request()), ProviderError,
         "an incomplete Responses API reply is rejected, not half-accepted",
         category=ErrorCategory.INCOMPLETE)

empty = LunaAdapter(make_settings(), client=FakeLunaClient([luna_response("   ")]))
c.raises(lambda: empty.generate(json_request()), ProviderError,
         "an empty Luna response is rejected", category=ErrorCategory.EMPTY_RESPONSE)

for status, expected in ((429, ErrorCategory.RATE_LIMIT), (500, ErrorCategory.OUTAGE),
                         (401, ErrorCategory.PROVIDER_AUTH), (400, ErrorCategory.BAD_REQUEST)):
    a = LunaAdapter(make_settings(), client=FakeLunaClient(errors=[openai_status_error(status)]))
    c.raises(lambda ad=a: ad.generate(json_request()), ProviderError,
             "Luna HTTP %d → %s" % (status, expected), category=expected)

a = LunaAdapter(make_settings(), client=FakeLunaClient(errors=[openai_timeout()]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "Luna timeout is classified as TIMEOUT", category=ErrorCategory.TIMEOUT)
a = LunaAdapter(make_settings(), client=FakeLunaClient(errors=[openai_connection_error()]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "Luna connection failure is classified as TRANSPORT", category=ErrorCategory.TRANSPORT)

a = LunaAdapter(make_settings(),
                client=FakeLunaClient(errors=[openai_status_error(429, "insufficient_quota")]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "an out-of-credit 429 is QUOTA, not a plain rate limit", category=ErrorCategory.QUOTA)

c.ok(not LunaAdapter(make_settings(openai_llm_api_key="")).is_configured(),
     "Luna without a key reports unconfigured")
c.ok(LunaAdapter(make_settings())._client is None,
     "constructing the Luna adapter builds no client and makes no call")


# ══ Haiku ═══════════════════════════════════════════════════════════════════

client = FakeHaikuClient([haiku_response(tool_input=DECK)])
adapter = HaikuAdapter(make_settings(), client=client)
result = adapter.generate(json_request())
sent = client.calls[0]

c.ok(sent["model"] == "claude-haiku-4-5", "Haiku is called with the Haiku 4.5 id")
c.ok("sonnet" not in json.dumps(sent).lower(), "Sonnet appears nowhere in a Haiku request")
c.ok(sent["tool_choice"] == {"type": "tool", "name": "wisdom_session"},
     "Haiku JSON workflows force the schema tool rather than asking for prose")
c.ok(sent["tools"][0]["input_schema"] == wisdom_schema(5, 4),
     "the shared schema is handed to Haiku unchanged")
c.ok(sent["system"][0]["cache_control"] == {"type": "ephemeral"},
     "the stable system block carries the prompt-cache marker")
c.ok(result.data == DECK, "Haiku's tool input is returned as parsed data")

client = FakeHaikuClient([haiku_response(text="A warm two-paragraph answer.")])
adapter = HaikuAdapter(make_settings(), client=client)
result = adapter.generate(text_request())
sent = client.calls[0]
c.ok(len(sent["system"]) == 2 and "cache_control" not in sent["system"][1],
     "the per-request context block is NOT cache-marked (it would rewrite the cache each call)")
c.ok("tools" not in sent, "Connect sends no schema — it is plain text")
c.ok(result.text == "A warm two-paragraph answer.", "Haiku text replies are returned as text")

client = FakeHaikuClient([haiku_response(tool_input=DECK, input_tokens=9000,
                                         cache_read=8000, cache_write=1000, output_tokens=2000)])
usage = HaikuAdapter(make_settings(), client=client).generate(json_request()).usage
c.ok(usage.cached_input_tokens == 8000 and usage.cache_write_tokens == 1000,
     "Anthropic prompt-cache read/write counts are captured")
c.ok(usage.reasoning_tokens == 0 and usage.visible_output_tokens == usage.output_tokens,
     "Haiku has no hidden reasoning — all output is visible")

sonnet = HaikuAdapter(make_settings(anthropic_llm_model="claude-sonnet-4-6"),
                      client=FakeHaikuClient([haiku_response(tool_input=DECK)]))
c.raises(lambda: sonnet.generate(json_request()), ProviderError,
         "the Haiku adapter refuses to run Sonnet", category=ErrorCategory.NOT_CONFIGURED)

trunc = HaikuAdapter(make_settings(), client=FakeHaikuClient(
    [haiku_response(text="half a d", stop_reason="max_tokens")]))
c.raises(lambda: trunc.generate(text_request()), ProviderError,
         "stop_reason max_tokens is an incomplete response", category=ErrorCategory.INCOMPLETE)

refuse = HaikuAdapter(make_settings(), client=FakeHaikuClient(
    [haiku_response(text="", stop_reason="refusal")]))
c.raises(lambda: refuse.generate(text_request()), ProviderError,
         "a Haiku refusal is detected", category=ErrorCategory.REFUSAL)

empty = HaikuAdapter(make_settings(), client=FakeHaikuClient([haiku_response(text="  ")]))
c.raises(lambda: empty.generate(text_request()), ProviderError,
         "an empty Haiku reply is rejected", category=ErrorCategory.EMPTY_RESPONSE)

# The old prompted-JSON path, still supported as a fallback.
fenced = HaikuAdapter(make_settings(), client=FakeHaikuClient(
    [haiku_response(text="```json\n" + DECK_JSON + "\n```")]))
c.ok(fenced.generate(json_request()).data == DECK,
     "a fenced JSON reply is still recovered when the tool is bypassed")

for status, expected in ((429, ErrorCategory.RATE_LIMIT), (529, ErrorCategory.OUTAGE),
                         (401, ErrorCategory.PROVIDER_AUTH)):
    a = HaikuAdapter(make_settings(), client=FakeHaikuClient(errors=[anthropic_status_error(status)]))
    c.raises(lambda ad=a: ad.generate(json_request()), ProviderError,
             "Haiku HTTP %d → %s" % (status, expected), category=expected)

a = HaikuAdapter(make_settings(),
                 client=FakeHaikuClient(errors=[anthropic_status_error(400, "credit balance is too low")]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "an out-of-credit 400 is INSUFFICIENT_CREDIT, not a bad request",
         category=ErrorCategory.INSUFFICIENT_CREDIT)

c.ok(HaikuAdapter(make_settings(anthropic_llm_api_key="", claude_api_key="legacy")).is_configured(),
     "the legacy CLAUDE_API_KEY still configures Haiku (an existing deploy keeps booting)")
c.ok(not HaikuAdapter(make_settings(anthropic_llm_api_key="", claude_api_key="")).is_configured(),
     "Haiku with no key at all reports unconfigured")


# ══ Qwen ════════════════════════════════════════════════════════════════════

client = FakeQwenClient([qwen_response(DECK_JSON)])
adapter = QwenAdapter(make_settings(qwen_model="qwen3-14b"), client=client)
result = adapter.generate(json_request())
sent = client.calls[0]

c.ok(sent["model"] == "qwen3-14b", "the Qwen model id is configuration-driven")
c.ok(NO_THINK in sent["messages"][0]["content"],
     "Qwen is told /no_think — a thinking pass would spend the output budget")
c.ok(sent["response_format"]["type"] == "json_schema",
     "Qwen JSON workflows use grammar-constrained structured output")
c.ok(sent["response_format"]["json_schema"]["schema"] == wisdom_schema(5, 4),
     "Qwen receives the same shared schema as the other two providers")
c.ok(result.data == DECK, "Qwen's JSON reply is parsed")
c.ok(result.usage.input_tokens == 1000 and result.usage.output_tokens == 500,
     "Qwen usage is normalized from prompt/completion tokens")

adapter = QwenAdapter(make_settings(qwen_base_url="https://tunnel.example.com/v1"))
c.ok(adapter.base_url == "https://tunnel.example.com/v1", "the Qwen base URL is configurable")
c.ok(not QwenAdapter(make_settings(qwen_api_key="")).is_configured(),
     "an unauthenticated Qwen endpoint is not accepted as configured")
c.ok(not QwenAdapter(make_settings(qwen_base_url="")).is_configured(),
     "Qwen without a base URL reports unconfigured")

# The Mac being asleep, the tunnel being down and llama-server being quit all
# look the same from Railway, and all mean "try someone else".
a = QwenAdapter(make_settings(), client=FakeQwenClient(errors=[openai_connection_error()]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "an unreachable Qwen endpoint is a TRANSPORT failure", category=ErrorCategory.TRANSPORT)
a = QwenAdapter(make_settings(), client=FakeQwenClient(errors=[openai_timeout()]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "a Qwen timeout is classified as TIMEOUT", category=ErrorCategory.TIMEOUT)

a = QwenAdapter(make_settings(), client=FakeQwenClient([qwen_response("half", finish_reason="length")]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "a length-truncated Qwen reply is incomplete", category=ErrorCategory.INCOMPLETE)
a = QwenAdapter(make_settings(), client=FakeQwenClient([qwen_response("")]))
c.raises(lambda: a.generate(json_request()), ProviderError,
         "an empty Qwen reply is rejected", category=ErrorCategory.EMPTY_RESPONSE)

client = FakeQwenClient([qwen_response("plain answer")])
QwenAdapter(make_settings(), client=client).generate(text_request())
c.ok(client.calls[0]["max_tokens"] == 600, "the Qwen timeout/token budget is passed through")

for url, local in (("http://127.0.0.1:8080/v1", True), ("http://localhost:8080", True),
                   ("http://192.168.1.9:8080", True), ("http://10.0.0.4:8080", True),
                   ("http://172.16.0.4:8080", True), ("http://172.42.0.4:8080", False),
                   ("https://qwen.getnibbler.com/v1", False)):
    c.ok(is_local_url(url) is local, "is_local_url(%s) == %s" % (url, local))

# Nothing here may pull model weights into the API process.
qwen_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "app", "services", "llm", "qwen.py")).read()
c.ok(not any(tok in qwen_src for tok in ("import torch", "llama_cpp", "from_pretrained",
                                         "hf_hub_download", "AutoModel")),
     "the Qwen adapter loads no weights into FastAPI/Railway")


# ══ cost arithmetic ═════════════════════════════════════════════════════════

# Sub-threshold deliberately: a 1M-token prompt would trip the long-prompt
# surcharge and cost $0.40, which is correct but not the base rate.
c.ok(estimate_cost("gpt-5.6-luna", Usage(input_tokens=200_000, output_tokens=0)) == 0.04,
     "Luna uncached input costs $0.20/1M")
c.ok(estimate_cost("gpt-5.6-luna", Usage(input_tokens=1_000_000)) == 0.40,
     "a 1M-token prompt is billed at the 2x long-prompt input rate")
c.ok(estimate_cost("gpt-5.6-luna", Usage(input_tokens=0, output_tokens=1_000_000)) == 1.20,
     "Luna output costs $1.20/1M")
c.ok(estimate_cost("gpt-5.6-luna",
                   Usage(input_tokens=1_000_000, cached_input_tokens=1_000_000)) == 0.02,
     "fully cached Luna input costs $0.02/1M")
c.ok(abs(estimate_cost("gpt-5.6-luna",
                       Usage(output_tokens=1000, visible_output_tokens=400,
                             reasoning_tokens=600)) - 0.0012) < 1e-9,
     "hidden reasoning tokens are billed at the output rate, not ignored")
c.ok(estimate_cost("qwen3-14b", Usage(input_tokens=999_999, output_tokens=999_999)) == 0.0,
     "self-hosted Qwen has no API cost (hardware and power are NOT counted as zero)")
c.ok(estimate_cost("gpt-5.6-luna", Usage(input_tokens=300_000, output_tokens=1_000_000))
     > estimate_cost("gpt-5.6-luna", Usage(input_tokens=270_000, output_tokens=1_000_000)) * 1.4,
     "the >272K-token long-prompt surcharge is applied")
c.ok("claude-haiku-4-5" in PRICING and "gpt-5.6-luna" in PRICING,
     "both paid models have pricing constants")

sys.exit(1 if c.finish() else 0)
