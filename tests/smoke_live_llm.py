"""
One real, paid call per provider. Owner-run, never automatic.

⚠️ **THIS SPENDS MONEY.** It is the only script in this repository that calls a
paid API, and it is double-gated: an explicit opt-in variable, and a provider
named on the command line. Named `smoke_*` so the `tests/test_*.py` runner
cannot pick it up.

The content is two sentences written for this file — no book text, no user data,
nothing copyrighted. Output is sanitized: token counts and estimated cost, never
the key, never the full response.

    RUN_LIVE_LLM_TESTS=1 .venv/bin/python tests/smoke_live_llm.py luna
    RUN_LIVE_LLM_TESTS=1 .venv/bin/python tests/smoke_live_llm.py haiku
    RUN_LIVE_LLM_TESTS=1 .venv/bin/python tests/smoke_live_llm.py qwen
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if os.environ.get("RUN_LIVE_LLM_TESTS") != "1":
    print("Skipped. This makes REAL, BILLED provider calls.\n"
          "  RUN_LIVE_LLM_TESTS=1 .venv/bin/python tests/smoke_live_llm.py <luna|haiku|qwen>")
    sys.exit(0)

provider = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
if provider not in ("luna", "haiku", "qwen"):
    print("Name one provider: luna, haiku or qwen.")
    sys.exit(2)

from app.config import get_settings  # noqa: E402
from app.services.llm.base import LLMRequest  # noqa: E402
from app.services.llm.errors import ProviderError  # noqa: E402
from app.services.llm.router import build_adapter  # noqa: E402
from app.services.llm.usage import estimate_cost  # noqa: E402

settings = get_settings()
adapter = build_adapter(provider, settings)
if adapter is None or not adapter.is_configured():
    print("%s is not configured — nothing was called." % provider)
    sys.exit(1)

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "headings": {"type": "array", "items": {"type": "string"},
                     "minItems": 2, "maxItems": 2},
    },
    "required": ["title", "headings"],
}

PASSAGE = (
    "The lighthouse keeper trimmed the wick every evening at dusk. "
    "In forty years he had never once let the light go out."
)

request = LLMRequest(
    operation="story_metadata",
    system="You name sections of text. Respond only with the requested JSON.",
    messages=[{"role": "user",
               "content": "Give a title and exactly 2 short headings for:\n\n" + PASSAGE}],
    max_visible_tokens=200,
    json_schema=SCHEMA,
    schema_name="live_smoke",
)

print("provider : %s" % provider)
print("model    : %s" % adapter.model)
if getattr(adapter, "reasoning", None):
    print("reasoning: %s (mode standard)" % adapter.reasoning)

try:
    result = adapter.generate(request)
except ProviderError as e:
    print("\nFAILED: %s / %s — %s" % (e.provider, e.category, e.detail))
    sys.exit(1)

data = result.data or {}
ok = isinstance(data.get("headings"), list) and len(data["headings"]) == 2
print("\n%s  schema honoured (exactly 2 headings)" % ("PASS " if ok else "FAIL "))
print("latency  : %d ms" % result.latency_ms)
u = result.usage
print("usage    : in=%d cached=%d out=%d (visible=%d reasoning=%d)"
      % (u.input_tokens, u.cached_input_tokens, u.output_tokens,
         u.visible_output_tokens, u.reasoning_tokens))
cost = estimate_cost(result.model, u)
print("est cost : $%.6f%s" % (cost, "  (self-hosted: API spend only)" if provider == "qwen" else ""))
sys.exit(0 if ok else 1)
