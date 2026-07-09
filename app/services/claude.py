import json
import re
from typing import Optional
import anthropic
from app.config import get_settings

settings = get_settings()

ONBOARDING_SYSTEM = """You are Nibbler, a warm, curious, gently mischievous cat companion in a learning app.
You are conducting a friendly onboarding interview to understand the user's goals, struggles, and learning habits.
Keep responses conversational, warm, and SHORT (2-4 sentences max). Ask ONE follow-up question at a time.
You're building a picture of: their background, current goals (career/health/relationships/mindset),
what they're struggling with, reading habits, time available daily, and preferred tone (motivating/gentle/direct).
After gathering enough info (5-7 exchanges), output a JSON object wrapped in <PROFILE>...</PROFILE> tags with keys:
name, goals (list), struggles, readingHabits, dailyTime, tonePreference, backgroundSummary"""

BITE_SYSTEM = """You are Nibbler's insight engine. Generate a personalized daily learning bite.
Respond ONLY with valid JSON — no markdown, no code fences. Use exactly these keys:
- title: catchy 5-8 word title
- insight: the main insight, 200-250 words, in the user's preferred tone
- reflection: single thought-provoking question (1 sentence)
- action: concrete, small action step (1-2 sentences)
- source: title of source material used (or "Your Nibbler Library" if synthesized)
- theme: one-word theme (e.g. Focus, Resilience, Habits, Mindset, Leadership)"""

SESSION_SYSTEM = """You are Nibbler's session engine. You build a daily "nibble session" — a tap-through
card deck — from excerpts of a book/article the user uploaded, personalized to their growth profile.

Respond ONLY with valid JSON, no markdown fences, matching exactly:
{
  "title": "short session title (5-9 words)",
  "chapter": "which part/theme of the source this draws from, e.g. 'On habits & identity'",
  "headline": "one arresting sentence that makes the user want to read (max 18 words)",
  "preview": "2-sentence preview of today's session (max 45 words)",
  "cards": [ ... exactly CARD_TARGET cards ... ],
  "quiz": [ ... exactly 3 items ... ]
}

Card shapes (kind determines shape):
- {"kind":"hook","eyebrow":"TODAY'S SESSION","title":"...","body":"..."}                    — 1st card, a story/scene/surprising fact from the source
- {"kind":"insight","eyebrow":"KEY IDEA","title":"...","body":"...","highlight":"optional pull-quote from the source"}
- {"kind":"quiz","eyebrow":"QUICK CHECK","title":"the question","options":[{"text":"...","correct":false},... 4 options, exactly 1 correct],"explanation":"..."}
- {"kind":"prompt","eyebrow":"TRY THIS TODAY"|"REFLECT & ACT"|"DAILY CHALLENGE","title":"...","body":"..."}
- {"kind":"summary","eyebrow":"SESSION SUMMARY","title":"The ideas from today's session.","body":"numbered recap"}

Deck structure: hook first, summary last, one interaction card (quiz OR prompt per the instruction you
receive) second-to-last, all remaining cards are insights. Card bodies: 90-160 words, warm, concrete,
faithful to the source excerpts — never invent facts not present in them. Use \\n\\n between paragraphs.

PERSONALIZATION (critical): the user's growth profile is provided. In EXACTLY ONE insight card, append a
final short paragraph that explicitly ties the idea to their stated goal or their answer about how they
approach new things — e.g. "You told Nibbler you take things step by step — this idea is exactly that kind
of small, repeatable move." Make it feel personally picked, never generic.

The separate top-level "quiz" array (3 multiple-choice questions, 4 options each, exactly 1 correct, with
explanations) tests today's session content — it is shown to the user TOMORROW, so questions must stand
alone without seeing the cards."""

STORY_SYSTEM = """You are Nibbler's story engine. The user reads a book in "story mode": sequential,
faithful, no personalization — the book itself, served in daily portions.

You receive the next raw excerpt of the book (extracted text, possibly messy). Clean it and split it into
a card deck. Respond ONLY with valid JSON, no markdown fences:
{
  "title": "short evocative title for today's portion (4-8 words)",
  "chapter": "PART N" (N provided in the instruction),
  "headline": "one line that sets the scene for today's reading (max 16 words)",
  "preview": "1-2 sentence teaser of today's portion (max 35 words)",
  "cards": [ {"kind":"story","eyebrow":"THE STORY CONTINUES","title":"short section heading","body":"the text"}, ... ]
}

Rules: preserve the author's actual words and order — you may fix broken hyphenation/whitespace and drop
page furniture (page numbers, headers), and lightly bridge a cut sentence, but never rewrite, summarize,
or add commentary. Split into the requested number of cards at natural pauses. First card's eyebrow is
"TODAY'S READING" instead of "THE STORY CONTINUES". End the last card's body with the sentence where the
excerpt ends — no cliffhanger text of your own."""


BOOK_CHAT_SYSTEM = """You are Nibbler, a warm, curious cat companion inside a learning app. The user is
chatting with ONE book from their own library. You are that book's voice and guide.

STRICT GROUNDING RULES (the product's core promise):
- Answer ONLY from the excerpts provided below. They are passages retrieved from the user's own
  uploaded copy of the book.
- Never use outside knowledge about this book, its author, or the topic — even if you know it.
- If the excerpts don't contain the answer, say so plainly and warmly, e.g. "I couldn't find that
  in the parts of the book I can see — try asking about …" and suggest something the excerpts DO cover.
- Quote or closely paraphrase the book when it helps; the user loves seeing their own book talk back.

STYLE: conversational, warm, concise — 2 short paragraphs max (under ~150 words). No headers, no
bullet walls. One gentle follow-up question at most, only when natural. Never mention "excerpts",
"chunks", or retrieval — just speak as someone who has read the book."""


class ClaudeService:
    def __init__(self, is_premium: bool = False):
        self.client = anthropic.Anthropic(api_key=settings.claude_api_key)
        self.model = settings.claude_model_paid if is_premium else settings.claude_model_free

    async def onboarding_reply(
        self,
        conversation_history: list[dict],
        user_message: str,
    ) -> dict:
        messages = [*conversation_history, {"role": "user", "content": user_message}]

        response = self.client.messages.create(
            model=self.model,
            max_tokens=512,
            system=ONBOARDING_SYSTEM,
            messages=messages,
        )
        text = response.content[0].text

        # Check if onboarding is complete (profile tag present)
        profile_match = re.search(r"<PROFILE>([\s\S]*?)</PROFILE>", text)
        if profile_match:
            try:
                raw = json.loads(profile_match.group(1).strip())
                profile = {
                    "name": raw.get("name", ""),
                    "goals": raw.get("goals", []),
                    "struggles": raw.get("struggles", ""),
                    "reading_habits": raw.get("readingHabits", ""),
                    "daily_time": raw.get("dailyTime", ""),
                    "tone_preference": raw.get("tonePreference", ""),
                    "background_summary": raw.get("backgroundSummary", ""),
                }
                reply = text.replace(profile_match.group(0), "").strip()
                return {"reply": reply, "profile": profile, "is_complete": True}
            except json.JSONDecodeError:
                pass

        return {"reply": text, "profile": None, "is_complete": False}

    async def generate_bite(self, profile: dict, context_chunks: list[str] = None) -> dict:
        context = "\n\n".join(context_chunks) if context_chunks else "Use your general knowledge about personal growth."
        user_msg = f"""User profile:
Name: {profile.get('name')}
Goals: {', '.join(profile.get('goals', []))}
Struggles: {profile.get('struggles', 'Not specified')}
Tone preference: {profile.get('tone_preference', 'warm')}
Daily time: {profile.get('daily_time', '5-10 minutes')}

Relevant library content:
{context}

Generate today's personalized bite."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=700,
            system=BITE_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = response.content[0].text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Fallback if JSON is malformed
            return {
                "title": "Today's Insight",
                "insight": text,
                "reflection": "What small step can you take today?",
                "action": "Reflect on this insight for 5 minutes.",
                "source": "Your Nibbler Library",
                "theme": "Growth",
            }

    # ── Session generation (July 2026) ────────────────────────────────────────

    @staticmethod
    def _parse_json(text: str) -> dict:
        clean = text.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean)
        # Tolerate stray prose around the JSON object
        start, end = clean.find("{"), clean.rfind("}")
        if start >= 0 and end > start:
            clean = clean[start:end + 1]
        return json.loads(clean)

    async def generate_wisdom_session(
        self,
        book_title: str,
        author: Optional[str],
        profile: dict,
        context_chunks: list[str],
        card_target: int,
        read_length: int,
    ) -> dict:
        """Personalized card-deck session from the user's own book excerpts."""
        interaction = {
            "analytical": "a QUIZ card (kind quiz, eyebrow QUICK CHECK)",
            "practical": 'a PROMPT card with eyebrow "TRY THIS TODAY"',
            "reflective": 'a PROMPT card with eyebrow "REFLECT & ACT"',
        }.get(profile.get("contentMode") or "practical", 'a PROMPT card with eyebrow "TRY THIS TODAY"')

        goal_bits = []
        if profile.get("aspirationUnderstanding"):
            goal_bits.append(f'their goal in their own words: they want {profile["aspirationUnderstanding"]}')
        elif profile.get("aspirationLabel"):
            goal_bits.append(f'their chosen goal: "{profile["aspirationLabel"]}"')
        if profile.get("lifeArea"):
            goal_bits.append(f'life area: {profile["lifeArea"]}')

        confidence_line = {
            "dive": 'they said "I dive straight in" when facing new things',
            "steps": 'they said "I take it step by step" when facing new things',
            "overwhelmed": 'they said "I get overwhelmed easily" when facing new things — keep the framing gentle and small',
            "depends": 'they said "depends on the topic" when facing new things',
        }.get(profile.get("confidenceStyle") or "steps", "")

        user_msg = f"""SOURCE: "{book_title}"{f' by {author}' if author else ''}

GROWTH PROFILE:
- {'; '.join(goal_bits) if goal_bits else 'general personal growth'}
- Confidence: {confidence_line}
- Interests: {', '.join(profile.get('interests') or [])}

CARD_TARGET: {card_target} cards total ({read_length}-minute read).
Interaction card (second-to-last): {interaction}.

SOURCE EXCERPTS (build the session ONLY from these):
{chr(10).join(f'--- excerpt {i+1} ---{chr(10)}{c}' for i, c in enumerate(context_chunks))}

Build today's session JSON now."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=SESSION_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        return self._parse_json(response.content[0].text)

    async def chat_with_book(
        self,
        book_title: str,
        author,
        excerpts: list,
        history: list,
        message: str,
    ) -> str:
        """Grounded chat: Nibbler answers only from this book's retrieved excerpts."""
        system = (
            BOOK_CHAT_SYSTEM
            + f"\n\nTHE BOOK: \"{book_title}\"{f' by {author}' if author else ''}"
            + "\n\nEXCERPTS FROM THE USER'S COPY:\n"
            + "\n".join(f"--- passage {i+1} ---\n{e}" for i, e in enumerate(excerpts))
        )
        # Keep the last few turns for continuity; roles must alternate for the API
        msgs = []
        for m in (history or [])[-8:]:
            role = m.get("role")
            content = (m.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                if msgs and msgs[-1]["role"] == role:
                    msgs[-1]["content"] += "\n" + content
                else:
                    msgs.append({"role": role, "content": content})
        if msgs and msgs[0]["role"] == "assistant":
            msgs = msgs[1:]
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1]["content"] += "\n" + message
        else:
            msgs.append({"role": "user", "content": message})

        response = self.client.messages.create(
            model=self.model,
            max_tokens=600,
            system=system,
            messages=msgs,
        )
        return response.content[0].text.strip()

    async def generate_story_session(
        self,
        book_title: str,
        author: Optional[str],
        excerpt: str,
        card_target: int,
        part_number: int,
    ) -> dict:
        """Sequential story-mode portion — faithful text, no personalization."""
        user_msg = f"""SOURCE: "{book_title}"{f' by {author}' if author else ''}
This is PART {part_number} of the user's sequential read.
Split into {card_target} cards.

RAW EXCERPT:
{excerpt}

Build today's portion JSON now."""

        response = self.client.messages.create(
            model=self.model,
            max_tokens=8000,
            system=STORY_SYSTEM,
            messages=[{"role": "user", "content": user_msg}],
        )
        result = self._parse_json(response.content[0].text)
        result["quiz"] = None
        return result
