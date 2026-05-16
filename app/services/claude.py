import json
import re
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
