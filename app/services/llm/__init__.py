"""
`LLMService` — the one boundary the rest of the backend talks to.

Routers and the session service call these four methods and get back the same
shapes they always did. They do not know, and must not learn, which provider
answered: no OpenAI, Anthropic or Qwen types cross this line, and no caller
branches on the model. That is the whole point of the refactor — switching
Nibbler from Luna to Haiku is a Railway variable, not a code change.

Replaces the old `ClaudeService`. The differences that matter:

  * **No `is_premium`.** It used to be a constructor argument that chose a
    model. Subscription tier now has nothing to do with which model runs;
    routing configuration decides that for everybody.
  * **Every JSON response is validated** — schema first at the provider, then
    the semantic rules in validation.py — before a caller ever sees it.
  * **Failure is bounded and observable**: at most two attempts per provider,
    each provider at most once, with usage and cost logged per attempt.

The token budgets below are VISIBLE-output sizes, carried over unchanged from
the previous implementation. Providers that hide reasoning inside the output
allowance add their own headroom (see luna.py) — passing these numbers to a
reasoning model as a total budget is exactly the bug that truncates a deck
halfway through.
"""

import logging
from typing import Any, Dict, List, Optional

from app.config import get_settings

from .base import LLMRequest, LLMResult
from .errors import ErrorCategory, ProviderError
from .prompts import (
    ASPIRATION_FALLBACK,
    ASPIRATION_SYSTEM,
    BOOK_CHAT_SYSTEM,
    SESSION_SYSTEM,
    STORY_SYSTEM,
    build_connect_context,
    build_story_user_message,
    build_wisdom_user_message,
    normalize_chat_history,
    quiz_target_for,
)
from .router import LLMConfigError, LLMRouter, validate_llm_settings
from .schemas import aspiration_schema, story_schema, wisdom_schema
from .usage import OP_ASPIRATION, OP_CONNECT, OP_STORY, OP_WISDOM
from .validation import (
    coerce_card_list,
    enforce_schema,
    shuffle_quiz_options,
    validate_aspiration,
    validate_chat_reply,
    validate_story,
    validate_wisdom,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LLMService",
    "get_llm_service",
    "LLMConfigError",
    "ProviderError",
    "ErrorCategory",
    "validate_llm_settings",
    "ASPIRATION_FALLBACK",
]

# ── visible-output budgets, per operation ───────────────────────────────────
# Unchanged from the Anthropic-only implementation so deck length, chat length
# and onboarding replies stay exactly as long as they are today.
ASPIRATION_MAX_TOKENS = 400
CONNECT_MAX_TOKENS = 600
STORY_BASE_TOKENS = 300
STORY_TOKENS_PER_CARD = 40
WISDOM_BASE_TOKENS = 1500
WISDOM_TOKENS_PER_CARD = 450          # ~90-160 words of body, plus its JSON
WISDOM_TOKENS_PER_QUIZ = 120
WISDOM_MAX_TOKENS_CEILING = 8000


def wisdom_max_tokens(card_target: int, quiz_target: int) -> int:
    return min(
        WISDOM_MAX_TOKENS_CEILING,
        WISDOM_BASE_TOKENS + card_target * WISDOM_TOKENS_PER_CARD
        + quiz_target * WISDOM_TOKENS_PER_QUIZ,
    )


def story_max_tokens(card_count: int) -> int:
    return STORY_BASE_TOKENS + card_count * STORY_TOKENS_PER_CARD


def interaction_kind_for(profile: Dict[str, Any]) -> str:
    """Which card the deck ends on before the summary.

    An analytical learner was promised a quiz; everyone else gets a prompt.
    Validated rather than assumed, because the shape of the deck is part of
    the personalization the user can see.
    """
    return "quiz" if (profile or {}).get("contentMode") == "analytical" else "prompt"


class LLMService:
    """Provider-neutral text generation for all four Nibbler workflows."""

    def __init__(self, settings=None, router: Optional[LLMRouter] = None):
        self._settings = settings or get_settings()
        self.router = router if router is not None else LLMRouter(self._settings)

    # ── onboarding ───────────────────────────────────────────────────────

    def interpret_aspiration(self, answer: str) -> dict:
        """Free-text onboarding answer → structured growth-profile seed.

        Never raises. Onboarding runs before an account exists and has no
        screen for "the model is down", so a total failure returns the same
        clarification fallback the app used to compute client-side. The user
        is asked to rephrase; nothing blocks.
        """
        schema = aspiration_schema()
        request = LLMRequest(
            operation=OP_ASPIRATION,
            system=ASPIRATION_SYSTEM,
            messages=[{"role": "user", "content": answer}],
            max_visible_tokens=ASPIRATION_MAX_TOKENS,
            json_schema=schema,
            schema_name="aspiration_profile",
            temperature=0.2,
        )

        def finalize(result: LLMResult) -> None:
            # Schema first, then the rules a schema cannot express. Supplying a
            # schema to a provider is not the same as receiving one back.
            enforce_schema(result.data or {}, schema, result.provider)
            validate_aspiration(result.data or {}, provider=result.provider)

        try:
            result = self.router.run(request, finalize)
        except ProviderError as e:
            logger.warning("interpret_aspiration failed (%s/%s) — serving fallback",
                           e.provider, e.category)
            return dict(ASPIRATION_FALLBACK)
        return result.data or dict(ASPIRATION_FALLBACK)

    # ── Wisdom sessions ──────────────────────────────────────────────────

    def generate_wisdom_session(
        self,
        book_title: str,
        author: Optional[str],
        profile: dict,
        context_chunks: List[str],
        card_target: int,
        read_length: int,
        image_options: Optional[str] = None,
    ) -> dict:
        """A personalized card deck built only from the user's own excerpts.

        `image_options` is the rendered shortlist of book figures this session
        may draw on (see services/image_select.py), or None when the book has
        none. It only changes the prompt and the schema: the ids that come back
        are suggestions, and session_service re-validates every one of them
        against the stored rows before anything reaches a card.

        Raises `ProviderError` when no provider produces a valid deck — the
        caller (session_service) turns that into its existing generation
        failure, because a half-built deck is worse than none.
        """
        quiz_target = quiz_target_for(read_length)
        interaction = interaction_kind_for(profile)
        with_images = bool(image_options)
        schema = wisdom_schema(card_target, quiz_target, with_images=with_images)
        user_message = build_wisdom_user_message(
            book_title=book_title, author=author, profile=profile,
            context_chunks=context_chunks, card_target=card_target,
            read_length=read_length,
        )
        if with_images:
            user_message += "\n\n" + image_options
        request = LLMRequest(
            operation=OP_WISDOM,
            system=SESSION_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            max_visible_tokens=wisdom_max_tokens(card_target, quiz_target),
            json_schema=schema,
            schema_name="wisdom_session",
        )

        def finalize(result: LLMResult) -> None:
            enforce_schema(result.data or {}, schema, result.provider)
            validate_wisdom(
                result.data or {}, provider=result.provider,
                card_target=card_target, quiz_target=quiz_target,
                interaction_kind=interaction,
                # The excerpts go in so pull-quotes can be checked against the
                # book rather than trusted because the prompt asked nicely.
                source_chunks=context_chunks,
            )

        result = self.router.run(request, finalize)
        session = result.data or {}
        # Strip the null placeholders the strict schema forces onto every card
        # BEFORE persisting, so stored decks keep the shape the app expects.
        session["cards"] = coerce_card_list(session.get("cards"))
        return shuffle_quiz_options(session)

    # ── Connect chat ─────────────────────────────────────────────────────

    def chat_with_book(
        self,
        book_title: str,
        author,
        excerpts: list,
        history: list,
        message: str,
    ) -> str:
        """Grounded chat: Nibbler answers only from this book's excerpts.

        Plain text, no schema. The grounding promise lives in the system prompt
        and the fact that the excerpts are the only source material supplied —
        that contract is identical for all three providers.
        """
        request = LLMRequest(
            operation=OP_CONNECT,
            system=BOOK_CHAT_SYSTEM,
            context=build_connect_context(book_title, author, excerpts),
            messages=normalize_chat_history(history, message),
            max_visible_tokens=CONNECT_MAX_TOKENS,
        )

        def finalize(result: LLMResult) -> None:
            validate_chat_reply(result.text, provider=result.provider)

        return validate_chat_reply(self.router.run(request, finalize).text, provider="router")

    # ── Story mode ───────────────────────────────────────────────────────

    def generate_story_metadata(
        self,
        book_title: str,
        author: Optional[str],
        card_bodies: List[str],
        part_number: int,
        image_options: Optional[str] = None,
    ) -> dict:
        """Titling for a Story portion the app has already cut, verbatim.

        The prose never round-trips through a model — it is sent only so each
        card can be named, and the model's own prose is discarded. That is what
        keeps a reader's book byte-for-byte the author's.

        Raises `ProviderError` on total failure; session_service already
        catches it and serves plain headings.
        """
        card_count = len(card_bodies)
        with_images = bool(image_options)
        schema = story_schema(card_count, with_images=with_images)
        user_message = build_story_user_message(
            book_title=book_title, author=author,
            card_bodies=card_bodies, part_number=part_number,
        )
        if with_images:
            # Only figures from the portion the reader has actually reached are
            # in this list — the spoiler guard is applied when the shortlist is
            # built, not left to the model's discretion.
            user_message += "\n\n" + image_options
        request = LLMRequest(
            operation=OP_STORY,
            system=STORY_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            max_visible_tokens=story_max_tokens(card_count),
            json_schema=schema,
            schema_name="story_metadata",
        )

        def finalize(result: LLMResult) -> None:
            enforce_schema(result.data or {}, schema, result.provider)
            validate_story(result.data or {}, provider=result.provider, card_count=card_count)

        result = self.router.run(request, finalize)
        return result.data or {}


def get_llm_service(**kwargs) -> LLMService:
    """Build the service. Not cached: settings are read per instance so a test
    can supply its own without poisoning a module-level singleton."""
    return LLMService(**kwargs)
