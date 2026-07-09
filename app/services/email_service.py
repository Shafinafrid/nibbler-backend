"""
Transactional email via Resend — the same account/domain the website's
contact form uses (from noreply@getnibbler.com).

Needs RESEND_API_KEY in the environment (Railway). Without it every send
is skipped with a log line, never an exception — email is a convenience
mirror, not a source of truth.
"""
import asyncio
import logging
import httpx
from typing import Optional
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

RESEND_URL = "https://api.resend.com/emails"


async def send_email(
    to: str,
    subject: str,
    html: str,
    text: str,
    reply_to: Optional[str] = None,
    from_name: str = "Nibbler",
) -> bool:
    if not settings.resend_api_key:
        logger.warning("RESEND_API_KEY not set — skipping email %r to %s", subject, to)
        return False

    payload = {
        "from": f"{from_name} <{settings.support_from_email}>",
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
    }
    if reply_to:
        payload["reply_to"] = reply_to

    # Resend free tier allows ~2 req/s — retry a 429 briefly before giving up.
    async with httpx.AsyncClient(timeout=20) as client:
        for attempt in range(1, 4):
            try:
                r = await client.post(
                    RESEND_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.resend_api_key}"},
                )
            except Exception as e:
                logger.error("Resend request failed: %s", e)
                return False
            if r.status_code < 300:
                return True
            if r.status_code == 429 and attempt < 3:
                await asyncio.sleep(0.35 * attempt)
                continue
            logger.error("Resend %s: %s", r.status_code, r.text[:300])
            return False
    return False
