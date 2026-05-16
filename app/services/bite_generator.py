from app.services.claude import ClaudeService
from app.services.embedding_service import EmbeddingService


class BiteGenerator:
    """
    Orchestrates daily bite generation:
    1. Search Pinecone for relevant content from user's library
    2. Pass context + profile to Claude to generate a personalised bite
    """

    def __init__(self, is_premium: bool = False):
        self.claude = ClaudeService(is_premium=is_premium)
        self.embeddings = EmbeddingService()

    async def generate(self, profile, user_id: str) -> dict:
        profile_dict = {
            "name": profile.name,
            "goals": profile.goals or [],
            "struggles": profile.struggles or "",
            "tone_preference": profile.tone_preference or "warm",
            "daily_time": profile.daily_time or "5-10 minutes",
            "background_summary": profile.background_summary or "",
        }

        # Build search query from user profile
        goal_str = ", ".join(profile_dict["goals"]) if profile_dict["goals"] else "personal growth"
        search_query = f"{goal_str}. {profile_dict['struggles']}. {profile_dict['background_summary']}"

        # Retrieve relevant library context
        context_chunks = await self.embeddings.search(
            query=search_query,
            user_id=user_id,
            top_k=5,
        )

        # Generate the bite
        bite_data = await self.claude.generate_bite(
            profile=profile_dict,
            context_chunks=context_chunks if context_chunks else None,
        )

        return bite_data
