"""
Push notification service.

Sends notifications via Expo's push API, which routes to APNs (iOS) and FCM (Android).
No native credentials needed on our end — Expo handles the APNs/FCM routing.

Expo Push API docs: https://docs.expo.dev/push-notifications/sending-notifications/
"""

import logging
from typing import Optional
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/exponent-push-token"

# APScheduler instance (started in main.py lifespan)
scheduler = AsyncIOScheduler()


async def send_push_notifications(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    expo_access_token: str = "",
) -> list[dict]:
    """
    Send push notifications to a list of Expo push tokens.
    Expo batches up to 100 tokens per request; we chunk accordingly.
    Returns a list of Expo push ticket responses.
    """
    if not tokens:
        return []

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if expo_access_token:
        headers["Authorization"] = f"Bearer {expo_access_token}"

    messages = [
        {
            "to": token,
            "title": title,
            "body": body,
            "sound": "default",
            "data": data or {},
        }
        for token in tokens
    ]

    # Expo allows max 100 messages per request
    chunk_size = 100
    tickets = []
    async with httpx.AsyncClient(timeout=10.0) as client:
        for i in range(0, len(messages), chunk_size):
            chunk = messages[i : i + chunk_size]
            try:
                resp = await client.post(EXPO_PUSH_URL, json=chunk, headers=headers)
                resp.raise_for_status()
                result = resp.json()
                tickets.extend(result.get("data", []))
            except Exception as exc:
                logger.error("Expo push batch failed: %s", exc)

    return tickets


async def _send_daily_bite_reminder(db_factory) -> None:
    """
    Scheduled job: called once per hour, sends "Your bite is ready" to every
    user whose preferred notification_hour matches the current UTC hour.
    """
    from datetime import datetime, timezone
    from app.models.push_token import PushToken
    from app.config import get_settings

    settings = get_settings()
    current_hour = datetime.now(timezone.utc).hour

    with db_factory() as db:
        rows = (
            db.query(PushToken)
            .filter(PushToken.notification_hour == current_hour)
            .all()
        )
        tokens = [r.token for r in rows]

    if not tokens:
        logger.debug("No push tokens for hour %d UTC", current_hour)
        return

    logger.info("Sending daily bite reminder to %d tokens (hour=%d UTC)", len(tokens), current_hour)
    await send_push_notifications(
        tokens=tokens,
        title="Your daily bite is ready 🐱",
        body="Nibbler has something fresh for you today.",
        data={"screen": "Home"},
        expo_access_token=getattr(settings, "expo_access_token", ""),
    )


def start_scheduler(db_factory) -> None:
    """
    Start the APScheduler with the daily notification job.
    db_factory should be a callable that returns a context-managed DB session.
    """
    if scheduler.running:
        return

    scheduler.add_job(
        _send_daily_bite_reminder,
        trigger="cron",
        minute=0,        # top of every hour
        kwargs={"db_factory": db_factory},
        id="daily_bite_reminder",
        replace_existing=True,
        misfire_grace_time=300,
    )
    scheduler.start()
    logger.info("Notification scheduler started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
