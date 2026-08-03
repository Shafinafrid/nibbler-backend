"""
Contract check against a REAL local Qwen server. Owner-run, never automatic.

Named `smoke_*` rather than `test_*` on purpose: the suite runner globs
`tests/test_*.py`, so this cannot be swept into a normal run. It also refuses to
start without an explicit opt-in.

It makes exactly one network call, to your own machine, and asks the smallest
question that still proves the thing worth proving — that llama-server honours a
JSON Schema and returns usage in the shape the adapter expects. Nothing here is
copyrighted or personal: the passage is two sentences written for this file.

    RUN_QWEN_CONTRACT_TEST=1 .venv/bin/python tests/smoke_qwen_contract.py

Start the server first — see docs/QWEN_LOCAL_SETUP.md.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if os.environ.get("RUN_QWEN_CONTRACT_TEST") != "1":
    print("Skipped. This makes a real call to your local Qwen server.\n"
          "  RUN_QWEN_CONTRACT_TEST=1 .venv/bin/python tests/smoke_qwen_contract.py")
    sys.exit(0)

from app.config import get_settings  # noqa: E402
from app.services.llm.base import LLMRequest  # noqa: E402
from app.services.llm.errors import ProviderError  # noqa: E402
from app.services.llm.qwen import QwenAdapter, is_local_url  # noqa: E402

settings = get_settings()
adapter = QwenAdapter(settings)

if not adapter.is_configured():
    print("Not configured. Set QWEN_BASE_URL, QWEN_API_KEY and QWEN_MODEL first.")
    print("  QWEN_BASE_URL is %s" % ("set" if settings.qwen_base_url else "EMPTY"))
    print("  QWEN_API_KEY  is %s" % ("set" if settings.qwen_api_key else "EMPTY"))
    sys.exit(1)

# Host only — a full URL could carry a token in a query string.
from urllib.parse import urlparse  # noqa: E402
host = urlparse(settings.qwen_base_url).hostname or "?"
print("endpoint : %s (%s)" % (host, "local" if is_local_url(settings.qwen_base_url) else "tunnelled"))
print("model    : %s" % settings.qwen_model)

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
    schema_name="contract_check",
)

try:
    result = adapter.generate(request)
except ProviderError as e:
    print("\nFAILED: %s / %s — %s" % (e.provider, e.category, e.detail))
    print("If this is a transport error the server or tunnel is not up.")
    sys.exit(1)

data = result.data or {}
checks = [
    ("returned an object", isinstance(data, dict)),
    ("honoured the schema keys", set(data) == {"title", "headings"}),
    ("honoured minItems/maxItems", isinstance(data.get("headings"), list)
     and len(data["headings"]) == 2),
    ("reported prompt tokens", result.usage.input_tokens > 0),
    ("reported completion tokens", result.usage.output_tokens > 0),
]
print()
failed = 0
for label, ok in checks:
    print("%s  %s" % ("PASS " if ok else "FAIL ", label))
    failed += 0 if ok else 1

print("\nlatency  : %d ms" % result.latency_ms)
print("usage    : in=%d out=%d" % (result.usage.input_tokens, result.usage.output_tokens))
print("api cost : $0.00 (self-hosted — hardware, power and tunnel are NOT counted)")
sys.exit(1 if failed else 0)
