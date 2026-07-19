import json
import logging
import re
from typing import Optional
import anthropic
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

SESSION_SYSTEM = """You are Nibbler's session engine. You build a daily "nibble session" — a tap-through
card deck — from excerpts of a book/article the user uploaded, personalized to their growth profile.

Respond ONLY with valid JSON, no markdown fences, matching exactly:
{
  "title": "short session title (5-9 words)",
  "chapter": "which part/theme of the source this draws from, e.g. 'On habits & identity'",
  "headline": "one arresting sentence that makes the user want to read (max 18 words)",
  "preview": "2-sentence preview of today's session (max 45 words)",
  "cards": [ ... exactly CARD_TARGET cards ... ],
  "quiz": [ ... exactly QUIZ_TARGET items ... ]
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

The separate top-level "quiz" array (QUIZ_TARGET multiple-choice questions, 4 options each, exactly 1
correct, with explanations) tests today's session content — it is shown to the user TOMORROW in the
Review tab, so questions must stand alone without seeing the cards. Keep quiz questions ≤ 20 words and
each option ≤ 12 words: they are answered as quick recall taps, not reading exercises. Explanations
stay 1-2 tight sentences."""

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


ASPIRATION_SYSTEM = """You are the onboarding interpreter for Nibbler, a personalized learning app.
The user was asked: "A year from now, what’s one thing you’d love to understand or be able to do better?"
Read their answer and return ONE JSON object that seeds their first growth profile.
Output ONLY valid JSON — no prose, no markdown fences.

CRITICAL RULE — needsClarification:
Set needsClarification to TRUE in two situations:
1. GIBBERISH — the answer is not real language: random keyboard characters ("askjdbaisdb", "fjfjfj"),
   only punctuation/numbers/emoji, or otherwise meaningless. Nibbler should admit it didn't catch that
   and ask them to say it again in a few words.
2. TOO VAGUE TO AIM — the answer is real words but gives NO concrete learning direction to build a
   profile from: "everything", "idk", "be better", "be happy", "success", "I want love", "life",
   "I don't know", "stuff". For these, warmly acknowledge what they said and ask them to elaborate —
   name the ambiguity if you can (e.g. love → romantic relationships? loving the people around them?
   self-love?).
An answer IS clear when it names a concrete domain, subject, skill, or activity with enough context to
aim at — even if short or grammatically rough ("learn to code", "understand money", "I want to learn
how to love people better"). Rough grammar is never a reason to clarify. Genuine ambiguity or emptiness is.

Fields:
- needsClarification (boolean): see CRITICAL RULE above.
- clarifyPrompt (string|null): ONLY if needsClarification is true — ONE warm sentence from Nibbler that
  (a) admits it didn't fully catch/understand that, and (b) asks them to say it differently or share a
  bit more (max ~25 words). Specific beats generic. Else null.
- lifeArea (string): short human-readable area. Map broadly and generously:
  business/startups/entrepreneurship → "Business & Entrepreneurship"
  coding/tech/software/AI → "Technology & Coding"
  finance/money/investing → "Personal Finance"
  health/fitness/diet → "Health & Fitness"
  relationships/people/communication → "Relationships"
  career/work/leadership → "Career Growth"
  creativity/art/writing/music → "Creativity"
  science/history/philosophy/world → "Understanding the World"
  focus/habits/productivity → "Focus & Productivity"
  spirituality/meaning/self → "Personal Growth"
- contentMode ("analytical" | "reflective" | "practical"):
   analytical = understanding facts/concepts/how things work
   reflective = meaning, emotions, relationships, self-understanding
   practical = building a skill/habit/behavior; doing something better
- motivation ("career" | "skill" | "habit" | "curiosity" | "prep")
- motivationType ("intrinsic" | "instrumental" | "mixed")
- goalOrientation ("mastery" | "summary" | "application")
- interests (array of 2-4 short topic tags WITHIN the life area, lowercase_snake)
- profileName (string): short, warm, user-facing name for this growth journey (max ~4 words)
- confirmation (string): ONE warm second-person sentence Nibbler shows to confirm it understood
  (max ~15 words)
- understanding (string): a restatement of the user's goal that completes the sentence
  "So, if I understand correctly, you want ..." — lowercase start, max ~18 words, plain and
  concrete, faithful to what they actually said (e.g. "to finally feel confident about
  investing your own money."). Never include the words "So, if I understand correctly".

Examples:

Input: "I want to understand businesses"
Output: {"needsClarification":false,"clarifyPrompt":null,"lifeArea":"Business & Entrepreneurship","contentMode":"analytical","motivation":"curiosity","motivationType":"mixed","goalOrientation":"mastery","interests":["business_strategy","entrepreneurship","how_companies_work"],"profileName":"Understanding How Business Works","confirmation":"Love that ambition — let’s start building your business mind.","understanding":"to understand how businesses really work, from strategy to what makes companies succeed."}

Input: "I want to understand of making businesses"
Output: {"needsClarification":false,"clarifyPrompt":null,"lifeArea":"Business & Entrepreneurship","contentMode":"analytical","motivation":"skill","motivationType":"mixed","goalOrientation":"application","interests":["entrepreneurship","startups","business_building"],"profileName":"Building a Business Mind","confirmation":"Love it — let’s explore what it really takes to build something.","understanding":"to learn what it actually takes to build a business of your own."}

Input: "I want to finally understand investing and stop being scared of my finances"
Output: {"needsClarification":false,"clarifyPrompt":null,"lifeArea":"Personal Finance","contentMode":"analytical","motivation":"skill","motivationType":"mixed","goalOrientation":"mastery","interests":["investing","personal_finance","money_mindset"],"profileName":"Getting Smart with Money","confirmation":"Love it — let’s make money feel a lot less scary.","understanding":"to finally understand investing and stop feeling scared of your own finances."}

Input: "be better at understanding the people I love and not messing up my relationships"
Output: {"needsClarification":false,"clarifyPrompt":null,"lifeArea":"Relationships","contentMode":"reflective","motivation":"curiosity","motivationType":"intrinsic","goalOrientation":"application","interests":["relationships","communication","emotional_intelligence"],"profileName":"Understanding the People I Love","confirmation":"Beautiful goal — let’s explore what makes relationships work.","understanding":"to better understand the people you love and take care of your relationships."}

Input: "i want to stop procrastinating and actually focus"
Output: {"needsClarification":false,"clarifyPrompt":null,"lifeArea":"Focus & Productivity","contentMode":"practical","motivation":"habit","motivationType":"intrinsic","goalOrientation":"application","interests":["focus","habits","procrastination"],"profileName":"Beating Procrastination","confirmation":"Let’s build the focus you’re after, one small step at a time.","understanding":"to stop procrastinating and build real, lasting focus."}

Input: "learn to code"
Output: {"needsClarification":false,"clarifyPrompt":null,"lifeArea":"Technology & Coding","contentMode":"practical","motivation":"skill","motivationType":"mixed","goalOrientation":"application","interests":["programming","coding","software_development"],"profileName":"Learning to Code","confirmation":"Let’s get you building things — one line at a time.","understanding":"to learn how to code and start building things yourself."}

Input: "askjdbaisdb"
Output: {"needsClarification":true,"clarifyPrompt":"Hmm, I didn't quite catch that — could you tell me in a few words what you'd love to learn or get better at?","lifeArea":"Personal Growth","contentMode":"practical","motivation":"curiosity","motivationType":"intrinsic","goalOrientation":"summary","interests":["self_improvement"],"profileName":"Growing Every Day","confirmation":"","understanding":""}

Input: "I want love"
Output: {"needsClarification":true,"clarifyPrompt":"Love is a big, beautiful goal — do you mean relationships, loving the people around you, or something else? Tell me a bit more.","lifeArea":"Relationships","contentMode":"reflective","motivation":"curiosity","motivationType":"intrinsic","goalOrientation":"application","interests":["relationships"],"profileName":"Understanding Love","confirmation":"","understanding":""}

Input: "I want to learn love. I want to know how to love people."
Output: {"needsClarification":false,"clarifyPrompt":null,"lifeArea":"Relationships","contentMode":"reflective","motivation":"curiosity","motivationType":"intrinsic","goalOrientation":"application","interests":["relationships","empathy","emotional_intelligence"],"profileName":"Learning to Love Well","confirmation":"What a beautiful thing to grow at — let's start.","understanding":"to learn how to truly love and care for the people in your life."}

Input: "everything"
Output: {"needsClarification":true,"clarifyPrompt":"I love the ambition! To point you somewhere real though — what's ONE area you'd pick first if you had to?","lifeArea":"Personal Growth","contentMode":"practical","motivation":"curiosity","motivationType":"intrinsic","goalOrientation":"summary","interests":["self_improvement"],"profileName":"Growing Every Day","confirmation":"","understanding":""}"""

# Returned when both interpretation attempts fail — mirrors the app's old
# client-side fallback so onboarding never blocks on a Claude outage.
ASPIRATION_FALLBACK = {
    "needsClarification": True,
    "clarifyPrompt": "Could you tell me a bit more about what you'd love to learn or do better?",
    "lifeArea": "Personal Growth",
    "contentMode": "practical",
    "motivation": "curiosity",
    "motivationType": "intrinsic",
    "goalOrientation": "summary",
    "interests": ["growth", "self_improvement"],
    "profileName": "Growing Every Day",
    "confirmation": "",
    "understanding": "",
}


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
        # Founder decision 2026-07-19: EVERY call uses the cheapest model
        # (Haiku), premium included — token cost control pre-launch. The
        # is_premium flag and claude_model_paid config stay as an easy
        # escape hatch if premium quality ever needs to come back.
        self.model = settings.claude_model_free

    def interpret_aspiration(self, answer: str) -> dict:
        """Turn a free-text onboarding aspiration into the structured profile seed.

        Ported from the app's aspirationInterpreter.js (July 2026) so the Anthropic
        key never ships in the client. One retry on failure; on double failure
        returns a clarification fallback so onboarding never blocks.
        """
        for attempt in (1, 2):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=400,
                    temperature=0.2,
                    system=ASPIRATION_SYSTEM,
                    messages=[{"role": "user", "content": answer}],
                )
                return self._parse_json(response.content[0].text)
            except Exception as e:
                logger.warning("interpret_aspiration attempt %d failed: %s", attempt, e)
        return dict(ASPIRATION_FALLBACK)

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

    def generate_wisdom_session(
        self,
        book_title: str,
        author: Optional[str],
        profile: dict,
        context_chunks: list[str],
        card_target: int,
        read_length: int,
    ) -> dict:
        """Personalized card-deck session from the user's own book excerpts."""
        # Review-deck size scales with the session length (founder spec
        # 2026-07-19): 5 min → 4 questions, 10 min → 7, 15 min → 9.
        quiz_target = {5: 4, 10: 7, 15: 9}.get(read_length, 4)
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
QUIZ_TARGET: {quiz_target} quiz questions.
Interaction card (second-to-last): {interaction}.

SOURCE EXCERPTS (build the session ONLY from these):
{chr(10).join(f'--- excerpt {i+1} ---{chr(10)}{c}' for i, c in enumerate(context_chunks))}

Build today's session JSON now."""

        response = self.client.messages.create(
            model=self.model,
            # Right-sized to the deck instead of a flat 8000: ~450 tokens per
            # card (90-160 words + JSON) plus headroom for the quiz/preview
            # (~120 tokens per quiz question).
            max_tokens=min(8000, 1500 + card_target * 450 + quiz_target * 120),
            # cache_control: the large static instruction block is cached
            # (~10% of input price on repeat calls within the TTL).
            system=[{"type": "text", "text": SESSION_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        return self._parse_json(response.content[0].text)

    def chat_with_book(
        self,
        book_title: str,
        author,
        excerpts: list,
        history: list,
        message: str,
    ) -> str:
        """Grounded chat: Nibbler answers only from this book's retrieved excerpts."""
        # Two system blocks: the static grounding rules are cached; the
        # per-question book/excerpt block is not (it changes with retrieval).
        system = [
            {"type": "text", "text": BOOK_CHAT_SYSTEM, "cache_control": {"type": "ephemeral"}},
            {
                "type": "text",
                "text": (
                    f"THE BOOK: \"{book_title}\"{f' by {author}' if author else ''}"
                    + "\n\nEXCERPTS FROM THE USER'S COPY:\n"
                    + "\n".join(f"--- passage {i+1} ---\n{e}" for i, e in enumerate(excerpts))
                ),
            },
        ]
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

    def generate_story_session(
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
            # Story cards carry the excerpt text through: budget ~600 tokens
            # per card plus headroom (excerpts are 1100-3300 words).
            max_tokens=min(8000, 2000 + card_target * 600),
            system=[{"type": "text", "text": STORY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        result = self._parse_json(response.content[0].text)
        result["quiz"] = None
        return result
