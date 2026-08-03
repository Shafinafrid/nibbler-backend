"""
Shared fakes for the LLM suites. Imported, never run directly.

Everything here is offline. No test in this repo may call OpenAI, Anthropic, a
Qwen endpoint, Voyage, Pinecone, S3 or Firebase, and none of them can: the fake
clients below are the only thing the adapters ever talk to, and settings are
built with `_env_file=None` so the real `.env` — which holds production
secrets — is never even read.

The response objects mimic the shape each SDK returns closely enough that the
adapters' extraction code is genuinely exercised: `output_text` and
`incomplete_details` for Luna, `content` blocks and `stop_reason` for Haiku,
`choices[0].message` for Qwen. A dict would have passed while proving nothing.
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hermetic  # noqa: E402,F401 — MUST precede any `app.` import; see hermetic.py

from app.config import Settings  # noqa: E402


# ── settings ────────────────────────────────────────────────────────────────

def make_settings(**overrides):
    """Settings built from explicit values only.

    Belt and braces: `_env_file=None` stops this call reading `.env`, and
    `hermetic` has already moved the process somewhere `.env` does not exist —
    so even a `get_settings()` deep inside an imported module cannot reach it.
    """
    base = dict(
        database_url="sqlite:///:memory:",
        firebase_project_id="test",
        openai_llm_api_key="test-luna-key",
        anthropic_llm_api_key="test-haiku-key",
        qwen_base_url="https://qwen.example.com/v1",
        qwen_api_key="test-qwen-key",
        qwen_context_size=32768,
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


# ── HTTP-shaped SDK exceptions ──────────────────────────────────────────────

def _httpx_response(status):
    import httpx
    return httpx.Response(status, request=httpx.Request("POST", "https://example.test/v1"))


def openai_status_error(status, message="boom"):
    import openai
    cls = openai.RateLimitError if status == 429 else openai.APIStatusError
    return cls(message, response=_httpx_response(status), body=None)


def openai_timeout():
    import openai
    import httpx
    return openai.APITimeoutError(request=httpx.Request("POST", "https://example.test/v1"))


def openai_connection_error():
    import openai
    import httpx
    return openai.APIConnectionError(request=httpx.Request("POST", "https://example.test/v1"))


def anthropic_status_error(status, message="boom"):
    import anthropic
    cls = anthropic.RateLimitError if status == 429 else anthropic.APIStatusError
    return cls(message, response=_httpx_response(status), body=None)


# ── Luna (OpenAI Responses API) ─────────────────────────────────────────────

class FakeLunaClient:
    """Records every call so a test can assert on the request, not just the reply."""

    def __init__(self, responses=None, errors=None):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls = []
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._errors:
            err = self._errors.pop(0)
            if err is not None:
                raise err
        if self._responses:
            return self._responses.pop(0)
        raise AssertionError("FakeLunaClient ran out of scripted responses")


def luna_response(text="", *, status="completed", refusal=False, incomplete_reason=None,
                  input_tokens=1000, cached=0, output_tokens=500, reasoning_tokens=100):
    content = [SimpleNamespace(type="refusal", refusal="I can't help with that")] if refusal \
        else [SimpleNamespace(type="output_text", text=text)]
    return SimpleNamespace(
        output_text=text,
        status=status,
        incomplete_details=SimpleNamespace(reason=incomplete_reason) if incomplete_reason else None,
        output=[SimpleNamespace(type="message", content=content)],
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            input_tokens_details=SimpleNamespace(cached_tokens=cached),
            output_tokens=output_tokens,
            output_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        ),
    )


# ── Haiku (Anthropic Messages API) ──────────────────────────────────────────

class FakeHaikuClient:
    def __init__(self, responses=None, errors=None):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._errors:
            err = self._errors.pop(0)
            if err is not None:
                raise err
        if self._responses:
            return self._responses.pop(0)
        raise AssertionError("FakeHaikuClient ran out of scripted responses")


def haiku_response(*, tool_input=None, text=None, stop_reason="end_turn",
                   input_tokens=1000, cache_read=0, cache_write=0, output_tokens=500):
    content = []
    if tool_input is not None:
        content.append(SimpleNamespace(type="tool_use", input=tool_input, name="response"))
    if text is not None:
        content.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


# ── Qwen (OpenAI-compatible chat completions) ───────────────────────────────

class FakeQwenClient:
    def __init__(self, responses=None, errors=None):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._errors:
            err = self._errors.pop(0)
            if err is not None:
                raise err
        if self._responses:
            return self._responses.pop(0)
        raise AssertionError("FakeQwenClient ran out of scripted responses")


def qwen_response(text="", *, finish_reason="stop", prompt_tokens=1000, completion_tokens=500):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason=finish_reason,
            message=SimpleNamespace(content=text),
        )],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        ),
    )


# ── content fixtures ────────────────────────────────────────────────────────

def valid_deck(card_target=5, quiz_target=4, interaction="prompt"):
    """A deck that satisfies every semantic rule, for tests that need a
    baseline to break one rule at a time."""
    cards = [{
        "kind": "hook", "eyebrow": "TODAY'S SESSION",
        "title": "The scene that started it", "body": "Body text.",
        "highlight": None, "options": None, "explanation": None,
    }]
    for i in range(card_target - 3):
        cards.append({
            "kind": "insight", "eyebrow": "KEY IDEA", "title": "Idea %d" % i,
            "body": "Body text.", "highlight": None, "options": None, "explanation": None,
        })
    if interaction == "quiz":
        cards.append({
            "kind": "quiz", "eyebrow": "QUICK CHECK", "title": "Which is true?",
            "body": None, "highlight": None,
            "options": _options(), "explanation": "Because.",
        })
    else:
        cards.append({
            "kind": "prompt", "eyebrow": "TRY THIS TODAY", "title": "Do this",
            "body": "Body text.", "highlight": None, "options": None, "explanation": None,
        })
    cards.append({
        "kind": "summary", "eyebrow": "SESSION SUMMARY",
        "title": "The ideas from today's session.", "body": "1. ...",
        "highlight": None, "options": None, "explanation": None,
    })
    return {
        "title": "A short session title here",
        "chapter": "On habits & identity",
        "headline": "One arresting sentence.",
        "preview": "Two sentences of preview.",
        "cards": cards,
        "quiz": [
            {"question": "Question %d?" % i, "options": _options(), "explanation": "Because."}
            for i in range(quiz_target)
        ],
    }


def _options():
    return [
        {"text": "Right", "correct": True},
        {"text": "Wrong 1", "correct": False},
        {"text": "Wrong 2", "correct": False},
        {"text": "Wrong 3", "correct": False},
    ]


def valid_aspiration():
    return {
        "needsClarification": False,
        "clarifyPrompt": None,
        "lifeArea": "Personal Finance",
        "contentMode": "analytical",
        "motivation": "skill",
        "motivationType": "mixed",
        "goalOrientation": "mastery",
        "interests": ["investing", "personal_finance"],
        "profileName": "Getting Smart with Money",
        "confirmation": "Love it.",
        "understanding": "to finally understand investing.",
    }


def valid_story(card_count=4):
    return {
        "title": "A quiet arrival",
        "headline": "The road bends toward the house.",
        "preview": "Today's portion.",
        "headings": ["Heading %d" % i for i in range(card_count)],
    }


# ── tiny assertion harness (matches this repo's existing suites) ────────────

class Checks:
    def __init__(self, title):
        self.title = title
        self.passed = 0
        self.failed = 0
        print("\n=== %s ===" % title)

    def ok(self, condition, label):
        if condition:
            self.passed += 1
            print("PASS  %s" % label)
        else:
            self.failed += 1
            print("FAIL  %s" % label)
        return bool(condition)

    def raises(self, fn, exc_type, label, category=None):
        try:
            fn()
        except exc_type as e:
            if category is not None and getattr(e, "category", None) != category:
                self.failed += 1
                print("FAIL  %s (expected category %s, got %s)"
                      % (label, category, getattr(e, "category", None)))
                return False
            self.passed += 1
            print("PASS  %s" % label)
            return True
        except Exception as e:  # noqa: BLE001
            self.failed += 1
            print("FAIL  %s (raised %s instead)" % (label, type(e).__name__))
            return False
        self.failed += 1
        print("FAIL  %s (nothing raised)" % label)
        return False

    def finish(self):
        total = self.passed + self.failed
        print("\n%s: %d/%d passed%s"
              % (self.title, self.passed, total,
                 "" if not self.failed else " — %d FAILED" % self.failed))
        return self.failed
