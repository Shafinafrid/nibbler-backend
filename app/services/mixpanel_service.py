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
MIXPANEL_ENGAGE_URL = "https://api.mixpanel.com/engage"


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


async def delete_profile(distinct_id: str) -> bool:
    """Task 7 (Aug 2026), account erasure: erase this user's stored Mixpanel
    PEOPLE PROFILE (name/email/plan/platform properties set via identify())
    using the Engage API's `$delete` operation. Returns False (never
    raises) on any failure or missing token — caller treats that as
    'needs retry', same as every other erasure artifact class.

    Scope note: this deletes the profile's stored PROPERTIES, not historical
    EVENTS already ingested (book_chat_message, session_generated, etc.) —
    full event-level erasure needs Mixpanel's separate, async GDPR Deletions
    API (a service-account-authenticated job, not the project-token ingestion
    API used here). Flagged as a known follow-up, not silently claimed done.
    """
    settings = get_settings()
    token = settings.mixpanel_token
    if not token:
        return False

    payload = [{
        "$token": token,
        "$distinct_id": distinct_id,
        "$delete": "",
        "$ignore_alias": True,
    }]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{MIXPANEL_ENGAGE_URL}?verbose=1",
                content=f"data={_encode(payload)}",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            logger.warning("Mixpanel profile delete HTTP %s for %s", resp.status_code, distinct_id)
            return False
        body = resp.json()
        # verbose=1 replies {"status": 1, ...} on success even for a
        # distinct_id with no existing profile — deleting nothing is still
        # a successful "this profile does not carry your data" outcome.
        return bool(body.get("status") == 1)
    except Exception as exc:
        logger.warning("Mixpanel profile delete failed for %s: %s", distinct_id, exc)
        return False
