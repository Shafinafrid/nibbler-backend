"""
Server-side Mixpanel event tracking via HTTP API.
Used for events that happen on the backend (e.g. bite_generated).
"""

import base64
import json
import logging
import time
from typing import Optional

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

MIXPANEL_TRACK_URL = "https://api.mixpanel.com/track"


def _encode(payload: list) -> str:
    return base64.b64encode(json.dumps(payload).encode()).decode()


async def track(
    event: str,
    distinct_id: str,
    properties: Optional[dict] = None,
) -> None:
    """
    Fire-and-forget: send a single event to Mixpanel.
    Failures are logged but never raised (analytics must not affect core logic).
    """
    settings = get_settings()
    token = settings.mixpanel_token
    if not token:
        return

    payload = [
        {
            "event": event,
            "properties": {
                "token": token,
                "distinct_id": distinct_id,
                "time": int(time.time()),
                "$lib": "nibbler-backend",
                **(properties or {}),
            },
        }
    ]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(
                f"{MIXPANEL_TRACK_URL}?verbose=1",
                content=f"data={_encode(payload)}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        logger.warning("Mixpanel track failed (%s): %s", event, exc)
