"""
The four workflows end to end through LLMService, plus telemetry hygiene and
the proof that AI image generation stays dark.

These are the tests that would catch a refactor quietly changing what a user
sees: a deck that no longer ends on a summary, a quiz with two right answers, a
Story portion whose headings drift out of step with its cards, an onboarding
screen that blanks out when a provider is down. The semantic rules are checked
by breaking exactly one thing at a time against a known-good fixture.

Two hygiene properties are also checked here because they have no other home:
prompts, excerpts and generated content must never reach the logs, and no
Nibble path may reach an image-generation API.

    .venv/bin/python tests/test_llm_workflows.py
"""

import copy
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from llm_fakes import (  # noqa: E402
    Checks, FakeHaikuClient, FakeLunaClient, FakeQwenClient,
    haiku_response, luna_response, make_settings, qwen_response,
    valid_aspiration, valid_deck, valid_story,
)

from app.services.llm import (  # noqa: E402
    ASPIRATION_FALLBACK, LLMService, interaction_kind_for, story_max_tokens, wisdom_max_tokens,
)
from app.services.llm.errors import ErrorCategory, ProviderError  # noqa: E402
from app.services.llm.haiku import HaikuAdapter  # noqa: E402
from app.services.llm.luna import LunaAdapter  # noqa: E402
from app.services.llm.qwen import QwenAdapter  # noqa: E402
from app.services.llm.router import LLMRouter, reset_shared_breaker  # noqa: E402
from app.services.llm.validation import (  # noqa: E402
    shuffle_quiz_options, validate_aspiration, validate_story, validate_wisdom,
)

c = Checks("LLM workflows")

PROFILE = {
    "aspirationUnderstanding": "to finally understand investing",
    "lifeArea": "Personal Finance",
    "interests": ["investing"],
    "confidenceStyle": "steps",
    "contentMode": "practical",
}
EXCERPTS = ["A distinctive passage about compound interest that must never be logged."]


def fresh_router(settings, adapters):
    """A router with a CLEAN circuit breaker.

    The breaker is process-wide by design — that is the whole point of the fix,
    since `LLMService()` is rebuilt on every request and a breaker that died
    with it never protected anything. The flip side is that a test which
    deliberately fails Luna leaves Luna's breaker open for every test after it,
    so each scenario resets first.
    """
    reset_shared_breaker()
    return LLMRouter(settings, adapters=adapters)


def service_with(provider, payloads, *, errors=None, mode="single", settings=None, **overrides):
    """An LLMService wired to one fake provider client."""
    settings = settings or make_settings(llm_routing_mode=mode, llm_active_provider=provider,
                                         **overrides)
    if provider == "luna":
        client = FakeLunaClient([luna_response(json.dumps(p) if isinstance(p, dict) else p)
                                 for p in payloads], errors=errors)
        adapter = LunaAdapter(settings, client=client)
    elif provider == "haiku":
        client = FakeHaikuClient(
            [haiku_response(tool_input=p) if isinstance(p, dict) else haiku_response(text=p)
             for p in payloads], errors=errors)
        adapter = HaikuAdapter(settings, client=client)
    else:
        client = FakeQwenClient([qwen_response(json.dumps(p) if isinstance(p, dict) else p)
                                 for p in payloads], errors=errors)
        adapter = QwenAdapter(settings, client=client)
    router = fresh_router(settings, adapters={provider: adapter})
    return LLMService(settings=settings, router=router), client


# ══ aspiration interpretation ═══════════════════════════════════════════════

for provider in ("luna", "haiku", "qwen"):
    svc, _ = service_with(provider, [valid_aspiration()])
    out = svc.interpret_aspiration("I want to understand investing")
    c.ok(out["lifeArea"] == "Personal Finance" and out["needsClarification"] is False,
         "%s: a valid aspiration returns the existing response shape" % provider)

svc, client = service_with("luna", ["not json at all", valid_aspiration()])
c.ok(svc.interpret_aspiration("x")["contentMode"] == "analytical",
     "a first malformed attempt is retried and the second succeeds")
c.ok(len(client.calls) == 2, "the retry is exactly one extra call")

svc, client = service_with("luna", ["nope", "still nope", "third"])
c.ok(svc.interpret_aspiration("x") == ASPIRATION_FALLBACK,
     "two failed attempts return ASPIRATION_FALLBACK — onboarding never blocks")
c.ok(len(client.calls) == 2, "single mode stops after its bounded retry")

settings = make_settings(llm_routing_mode="single", llm_active_provider="luna")
refusing = LunaAdapter(settings, client=FakeLunaClient([luna_response("", refusal=True)]))
svc = LLMService(settings=settings, router=fresh_router(settings, adapters={"luna": refusing}))
c.ok(svc.interpret_aspiration("x") == ASPIRATION_FALLBACK,
     "a refusal still yields the safe fallback rather than an exception")

incomplete = LunaAdapter(settings, client=FakeLunaClient(
    [luna_response('{"lifeArea": "Per', status="incomplete", incomplete_reason="max_output_tokens")]))
svc = LLMService(settings=settings, router=fresh_router(settings, adapters={"luna": incomplete}))
c.ok(svc.interpret_aspiration("x") == ASPIRATION_FALLBACK, "an incomplete reply falls back safely")

svc, _ = service_with("luna", ["   "])
c.ok(svc.interpret_aspiration("x") == ASPIRATION_FALLBACK, "an empty reply falls back safely")

# Cross-provider fallback for aspiration.
settings = make_settings()
luna = LunaAdapter(settings, client=FakeLunaClient(errors=[
    __import__("tests.llm_fakes", fromlist=["x"]).openai_status_error(500)]))
haiku = HaikuAdapter(settings, client=FakeHaikuClient([haiku_response(tool_input=valid_aspiration())]))
svc = LLMService(settings=settings, router=fresh_router(settings, adapters={"luna": luna, "haiku": haiku}))
c.ok(svc.interpret_aspiration("x")["lifeArea"] == "Personal Finance",
     "an eligible Luna failure falls back to Haiku for aspiration")

bad = copy.deepcopy(valid_aspiration())
bad["contentMode"] = "vibes"
c.raises(lambda: validate_aspiration(bad, provider="luna"), ProviderError,
         "an out-of-enum contentMode is a validation failure")
bad = copy.deepcopy(valid_aspiration())
bad.update({"needsClarification": True, "clarifyPrompt": ""})
c.raises(lambda: validate_aspiration(bad, provider="luna"), ProviderError,
         "needsClarification with an empty clarifyPrompt is rejected (blank onboarding screen)")


# ══ Wisdom sessions ═════════════════════════════════════════════════════════

for provider in ("luna", "haiku", "qwen"):
    svc, client = service_with(provider, [valid_deck(5, 4)])
    deck = svc.generate_wisdom_session("Book", "Author", PROFILE, EXCERPTS, 5, 5)
    c.ok(len(deck["cards"]) == 5 and len(deck["quiz"]) == 4,
         "%s: a valid deck preserves the existing contract" % provider)
    c.ok(deck["cards"][0]["kind"] == "hook" and deck["cards"][-1]["kind"] == "summary",
         "%s: deck ordering survives the round trip" % provider)
    c.ok("options" not in deck["cards"][0],
         "%s: schema null placeholders are stripped before persistence" % provider)

svc, client = service_with("luna", [valid_deck(5, 4)])
svc.generate_wisdom_session("Book", "Author", PROFILE, EXCERPTS, 5, 5)
sent = client.calls[0]
c.ok(sent["text"]["format"]["schema"]["properties"]["cards"]["minItems"] == 5,
     "the schema carries the exact card target for this request")
c.ok(sent["text"]["format"]["schema"]["properties"]["quiz"]["maxItems"] == 4,
     "the schema carries the exact quiz target for this request")
c.ok(sent["reasoning"]["effort"] == "low", "Wisdom generation runs Luna at low reasoning")

c.ok(wisdom_max_tokens(5, 4) == 4230 and wisdom_max_tokens(8, 7) == 5940
     and wisdom_max_tokens(12, 9) == 7980,
     "the visible-output budgets are unchanged from the previous implementation")
c.ok(sent["max_output_tokens"] > wisdom_max_tokens(5, 4),
     "Luna gets headroom above the visible budget for hidden reasoning")

# One broken rule at a time, against a known-good deck.
def wisdom_fails(mutate, label, card_target=5, quiz_target=4, interaction="prompt"):
    deck = valid_deck(card_target, quiz_target, interaction)
    mutate(deck)
    c.raises(lambda: validate_wisdom(deck, provider="luna", card_target=card_target,
                                     quiz_target=quiz_target, interaction_kind=interaction),
             ProviderError, label)


wisdom_fails(lambda d: d["cards"].pop(), "a short deck is rejected")
wisdom_fails(lambda d: d["cards"].append(d["cards"][1]), "an over-long deck is rejected")
wisdom_fails(lambda d: d["quiz"].pop(), "a short review quiz is rejected")
wisdom_fails(lambda d: d["cards"][0].__setitem__("kind", "insight"),
             "a deck not starting with a hook is rejected")
wisdom_fails(lambda d: d["cards"][-1].__setitem__("kind", "insight"),
             "a deck not ending with a summary is rejected")
wisdom_fails(lambda d: d["cards"][-2].__setitem__("kind", "insight"),
             "a missing interaction card is rejected")
wisdom_fails(lambda d: d["cards"][1].__setitem__("kind", "story"),
             "an unsupported card kind is rejected")
wisdom_fails(lambda d: d["cards"][1].__setitem__("kind", "hook"),
             "a second hook in the middle of the deck is rejected")
wisdom_fails(lambda d: [o.__setitem__("correct", False) for o in d["quiz"][0]["options"]],
             "a quiz question with no correct answer is rejected")
wisdom_fails(lambda d: [o.__setitem__("correct", True) for o in d["quiz"][0]["options"]],
             "a quiz question with several correct answers is rejected")
wisdom_fails(lambda d: d["quiz"][0]["options"].pop(),
             "a quiz question with three options is rejected")
wisdom_fails(lambda d: d["cards"][1].__setitem__("body", "  "),
             "an insight card with an empty body is rejected")
wisdom_fails(lambda d: d.__setitem__("headline", ""), "a deck with no headline is rejected")
wisdom_fails(lambda d: [o.__setitem__("correct", True) for o in d["cards"][-2]["options"]],
             "an in-deck quiz card with two correct options is rejected",
             interaction="quiz")

deck = valid_deck(5, 4, "quiz")
c.ok(validate_wisdom(deck, provider="luna", card_target=5, quiz_target=4,
                     interaction_kind="quiz") is deck,
     "an analytical profile's quiz-ending deck validates")
c.raises(lambda: validate_wisdom(valid_deck(5, 4, "prompt"), provider="luna", card_target=5,
                                 quiz_target=4, interaction_kind="quiz"),
         ProviderError, "a prompt card where a quiz was promised is rejected")
c.ok(interaction_kind_for({"contentMode": "analytical"}) == "quiz",
     "an analytical profile asks for a quiz card")
c.ok(interaction_kind_for({"contentMode": "reflective"}) == "prompt",
     "a reflective profile asks for a prompt card")

# Shuffling must move options without moving the answer.
deck = valid_deck(5, 4, "quiz")
before = {o["text"] for o in deck["quiz"][0]["options"] if o["correct"]}
positions = set()
for _ in range(40):
    d = shuffle_quiz_options(valid_deck(5, 4, "quiz"))
    positions.add(next(i for i, o in enumerate(d["quiz"][0]["options"]) if o["correct"]))
    after = {o["text"] for o in d["quiz"][0]["options"] if o["correct"]}
    if after != before:
        break
c.ok(after == before, "shuffling never changes WHICH option is correct")
c.ok(len(positions) > 1, "shuffling actually moves the correct answer around")
c.ok(all(len(shuffle_quiz_options(valid_deck(5, 4, "quiz"))["quiz"][i]["options"]) == 4
         for i in range(4)), "shuffling preserves all four options")

# Both providers must be asked the same thing.
settings = make_settings()
luna_client = FakeLunaClient(errors=[__import__("tests.llm_fakes", fromlist=["x"]).openai_status_error(503)])
haiku_client = FakeHaikuClient([haiku_response(tool_input=valid_deck(5, 4))])
svc = LLMService(settings=settings, router=fresh_router(settings, adapters={
    "luna": LunaAdapter(settings, client=luna_client),
    "haiku": HaikuAdapter(settings, client=haiku_client),
}))
deck = svc.generate_wisdom_session("Book", "Author", PROFILE, EXCERPTS, 5, 5)
c.ok(len(deck["cards"]) == 5, "a Luna outage falls back to Haiku and still returns a valid deck")
c.ok(haiku_client.calls[0]["system"][0]["text"] == luna_client.calls[0]["instructions"],
     "the fallback provider receives the identical system prompt")
c.ok(haiku_client.calls[0]["messages"][0]["content"] == luna_client.calls[0]["input"][0]["content"],
     "the fallback provider receives the identical user message and excerpts")

svc, _ = service_with("luna", [valid_deck(3, 4)])
c.raises(lambda: svc.generate_wisdom_session("B", None, PROFILE, EXCERPTS, 5, 5),
         ProviderError, "a deck of the wrong size raises rather than persisting")


# ══ Story metadata ══════════════════════════════════════════════════════════

BODIES = ["Paragraph one.\n\nParagraph two.", "Next card.", "Third.", "Fourth."]

for provider in ("luna", "haiku", "qwen"):
    svc, _ = service_with(provider, [valid_story(4)])
    meta = svc.generate_story_metadata("Book", "Author", BODIES, 3)
    c.ok(len(meta["headings"]) == 4, "%s: one heading per card" % provider)

c.raises(lambda: validate_story(valid_story(3), provider="luna", card_count=4),
         ProviderError, "too few headings is rejected (cards would be mislabelled)")
c.raises(lambda: validate_story(valid_story(5), provider="luna", card_count=4),
         ProviderError, "too many headings is rejected")

svc, client = service_with("luna", [valid_story(4)])
svc.generate_story_metadata("Book", "Author", BODIES, 3)
sent_text = json.dumps(client.calls[0]["input"])
c.ok(client.calls[0]["text"]["format"]["schema"]["properties"]["headings"]["minItems"] == 4,
     "the story schema pins the heading count to the card count")
c.ok("headings" in json.dumps(client.calls[0]["text"]["format"]["schema"]["properties"]),
     "the model is asked only for metadata and headings")
c.ok("body" not in client.calls[0]["text"]["format"]["schema"]["properties"]
     and "cards" not in client.calls[0]["text"]["format"]["schema"]["properties"],
     "the story schema has no field for prose — card bodies stay server-owned")
c.ok(story_max_tokens(4) == 460, "the story token budget is unchanged (300 + 40/card)")

# session_service already catches a story failure and serves plain headings;
# this proves the exception it catches is still the one raised.
svc, _ = service_with("luna", ["garbage", "garbage"])
c.raises(lambda: svc.generate_story_metadata("Book", None, BODIES, 1), ProviderError,
         "a total story failure raises ProviderError for session_service's local fallback")

import inspect  # noqa: E402
from app.services import session_service  # noqa: E402
story_src = inspect.getsource(session_service.generate_session_for_item)
c.ok("serving plain headings" in story_src and "except Exception" in story_src,
     "session_service still falls back to plain headings when metadata fails")
c.ok('"kind": "story"' in story_src and '"body": body' in story_src,
     "story card bodies still come from the server-side split, not the model")


# ══ Connect chat ════════════════════════════════════════════════════════════

for provider in ("luna", "haiku", "qwen"):
    svc, client = service_with(provider, ["A warm grounded answer."])
    reply = svc.chat_with_book("Book", "Author", EXCERPTS, [], "What about compounding?")
    c.ok(reply == "A warm grounded answer.", "%s: a plain-text answer is extracted" % provider)

svc, client = service_with("haiku", ["ok"])
svc.chat_with_book("Book", "Author", EXCERPTS,
                   [{"role": "user", "content": "earlier q"},
                    {"role": "assistant", "content": "earlier a"}],
                   "new question")
sent = client.calls[0]
c.ok(EXCERPTS[0] in sent["system"][1]["text"], "Connect is grounded in the retrieved passages")
c.ok(sent["messages"][-1]["content"].endswith("new question"),
     "the new question is the final user turn")
c.ok([m["role"] for m in sent["messages"]] == ["user", "assistant", "user"],
     "history is normalized into strictly alternating roles")
c.ok("tools" not in sent, "Connect sends no schema")

long_history = [{"role": "user", "content": "q%d" % i} if i % 2 == 0
                else {"role": "assistant", "content": "a%d" % i} for i in range(20)]
svc, client = service_with("haiku", ["ok"])
svc.chat_with_book("Book", None, EXCERPTS, long_history, "latest")
c.ok(len(client.calls[0]["messages"]) <= 9,
     "only the intended recent history is sent, not the whole conversation")

svc, _ = service_with("luna", ["   "])
c.raises(lambda: svc.chat_with_book("B", None, EXCERPTS, [], "q"), ProviderError,
         "an empty Connect reply is an error, not a blank message")

settings = make_settings()
luna_client = FakeLunaClient(errors=[__import__("tests.llm_fakes", fromlist=["x"]).openai_status_error(429)])
haiku_client = FakeHaikuClient([haiku_response(text="fallback answer")])
svc = LLMService(settings=settings, router=fresh_router(settings, adapters={
    "luna": LunaAdapter(settings, client=luna_client),
    "haiku": HaikuAdapter(settings, client=haiku_client),
}))
c.ok(svc.chat_with_book("B", None, EXCERPTS, [], "q") == "fallback answer",
     "a rate-limited Luna falls back to Haiku for Connect")

# Haiku is genuinely registered in the router here — if single mode leaked, it
# would answer and the spy would record a call.
single = make_settings(llm_routing_mode="single", llm_active_provider="luna")
haiku_spy = FakeHaikuClient([haiku_response(text="should never be used")])
svc = LLMService(settings=single, router=fresh_router(single, adapters={
    "luna": LunaAdapter(single, client=FakeLunaClient([luna_response("only luna")])),
    "haiku": HaikuAdapter(single, client=haiku_spy),
}))
c.ok(svc.chat_with_book("B", None, EXCERPTS, [], "q") == "only luna",
     "single mode keeps Connect on the selected provider")
c.ok(not haiku_spy.calls, "single mode never touches the registered fallback provider")


# ══ telemetry hygiene ═══════════════════════════════════════════════════════

class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


cap = Capture()
root = logging.getLogger("app.services.llm")
root.addHandler(cap)
root.setLevel(logging.INFO)

svc, _ = service_with("luna", [valid_deck(5, 4)])
svc.generate_wisdom_session("Book", "Author", PROFILE, EXCERPTS, 5, 5)
blob = "\n".join(cap.lines)

c.ok("llm_attempt" in blob, "an llm_attempt event is emitted")
c.ok("llm_usage" in blob, "an llm_usage event is emitted")
c.ok('"final_provider": "luna"' in blob, "the final provider used is recorded")
c.ok('"estimated_cost_usd"' in blob and '"reasoning_tokens"' in blob,
     "usage and estimated cost are recorded per attempt")
c.ok('"operation": "wisdom_session"' in blob, "the stable operation label is used")

leaks = [
    EXCERPTS[0], "compound interest", "understand investing",
    "The scene that started it", "SESSION SUMMARY", "Body text.",
    PROFILE["aspirationUnderstanding"],
]
c.ok(not any(leak in blob for leak in leaks),
     "no excerpt, profile, prompt or card content appears in the logs")
c.ok("test-luna-key" not in blob and "test-haiku-key" not in blob,
     "no API key appears in the logs")

cap.lines = []
settings = make_settings()
luna_client = FakeLunaClient(errors=[__import__("tests.llm_fakes", fromlist=["x"]).openai_status_error(500)])
haiku_client = FakeHaikuClient([haiku_response(tool_input=valid_deck(5, 4))])
svc = LLMService(settings=settings, router=fresh_router(settings, adapters={
    "luna": LunaAdapter(settings, client=luna_client),
    "haiku": HaikuAdapter(settings, client=haiku_client),
}))
svc.generate_wisdom_session("Book", "Author", PROFILE, EXCERPTS, 5, 5)
blob = "\n".join(cap.lines)
c.ok("llm_fallback" in blob, "a fallback emits its own event")
c.ok('"success": false' in blob and '"success": true' in blob,
     "the failed attempt is recorded as failed, not folded into the success")
c.ok('"attempts": 2' in blob, "the whole logical request reports both attempts")
c.ok('"fell_back": true' in blob, "the request is marked as having fallen back")
c.ok('"error_category": "outage"' in blob, "the failure category is recorded")

# A response that arrived and then failed validation was still generated, and
# still billed. Its tokens must be in the logical request's total.
cap.lines = []
svc, _ = service_with("luna", [valid_deck(3, 4), valid_deck(5, 4)])
svc.generate_wisdom_session("Book", "Author", PROFILE, EXCERPTS, 5, 5)
events = [json.loads(l.split(" ", 1)[1]) for l in cap.lines if l.startswith("llm_")]
failed_attempt = next(e for e in events if e.get("success") is False)
summary = next(e for e in events if "total_cost_usd" in e)
c.ok(failed_attempt.get("estimated_cost_usd", 0) > 0,
     "an attempt rejected by validation still reports the tokens it burned")
c.ok(summary["total_cost_usd"] > failed_attempt["estimated_cost_usd"],
     "the request total includes the retried attempt, not just the winning one")
c.ok(summary["attempts"] == 2, "both attempts are counted in the request summary")
root.removeHandler(cap)


# ══ AI image generation stays dark ══════════════════════════════════════════

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

runtime_files = []
for root_dir, dirs, files in os.walk(os.path.join(BACKEND, "app")):
    dirs[:] = [d for d in dirs if d != "__pycache__"]
    for f in files:
        if f.endswith(".py"):
            runtime_files.append(os.path.join(root_dir, f))
runtime_files.append(os.path.join(BACKEND, "main.py"))

importers = [p for p in runtime_files
             if "image_gen" in open(p).read()
             and os.path.basename(p) not in ("image_gen.py", "config.py")]
c.ok(not importers, "no runtime module imports or calls image_gen (found: %s)" % importers)

for name, svc_factory in (
    ("luna", lambda: service_with("luna", [valid_deck(5, 4)])),
    ("haiku", lambda: service_with("haiku", [valid_deck(5, 4)])),
    ("qwen", lambda: service_with("qwen", [valid_deck(5, 4)])),
):
    svc, _ = svc_factory()
    deck = svc.generate_wisdom_session("Book", "Author", PROFILE, EXCERPTS, 5, 5)
    c.ok(all("image" not in card for card in deck["cards"]),
         "%s configuration produces a text-only deck — no image generation" % name)

# Tier must not change the outcome: the same routing serves everyone.
sess_src = inspect.getsource(session_service)
c.ok("ClaudeService" not in sess_src, "session_service no longer references ClaudeService")
c.ok("is_premium=" not in sess_src,
     "no premium flag is passed into text generation — tier does not pick a model")

image_settings = make_settings(openai_llm_api_key="sk-luna-only")
c.ok(image_settings.openai_api_key == "",
     "setting the Luna key leaves the image-generation key untouched")
c.ok(image_settings.openai_llm_api_key != image_settings.openai_api_key,
     "the Luna credential and the image credential are separate settings")

llm_dir = os.path.join(BACKEND, "app", "services", "llm")
llm_src = "\n".join(open(os.path.join(llm_dir, f)).read()
                    for f in os.listdir(llm_dir) if f.endswith(".py"))
c.ok("images/generations" not in llm_src and "gpt-image" not in llm_src,
     "no image API endpoint or image model appears anywhere in the llm package")
c.ok("openai_image_api_key" not in llm_src.lower(),
     "no image-generation credential is required by the llm package")

sys.exit(1 if c.finish() else 0)
