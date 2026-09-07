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
MIXPANEL_DELETION_URL = "https://mixpanel.com/api/app/data-deletions/v3.0/"


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

    Scope note: this deletes only profile properties. The account-erasure
    state machine separately calls ``delete_historical_events`` below and
    retains its durable row until Mixpanel's async GDPR job reports success.
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
        return bool(body.get("status") == 1)
    except Exception as exc:
        logger.warning("Mixpanel profile delete failed for %s: %s", distinct_id, exc)
        return False


async def delete_historical_events(distinct_id: str, tracking_id: str = None) -> tuple[bool, str, str]:
    """Submit or poll Mixpanel's asynchronous end-user data deletion.

    Returns (complete, tracking_id, status). A successfully accepted but
    unfinished job is deliberately not complete; the durable account-erasure
    scheduler calls this again until Mixpanel confirms completion.
    """
    settings = get_settings()
    bearer = settings.mixpanel_gdpr_bearer_token
    token = settings.mixpanel_token
    if not bearer or not token:
        return False, tracking_id, "credentials_missing"
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    params = {"token": token}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if tracking_id:
                resp = await client.get(
                    f"{MIXPANEL_DELETION_URL}{tracking_id}", headers=headers, params=params,
                )
            else:
                resp = await client.post(
                    MIXPANEL_DELETION_URL,
                    headers={**headers, "Content-Type": "application/json"},
                    params=params,
                    json={"compliance_type": "GDPR", "distinct_ids": [distinct_id]},
                )
            resp.raise_for_status()
            body = resp.json()

        # v3 wraps POST results in a one-element list and GET results in an
        # object. The top-level status is merely "ok"; lifecycle state lives
        # inside results (PENDING/STAGING/STARTED/SUCCESS/etc.).
        results = body.get("results") if isinstance(body, dict) else None
        if isinstance(results, list):
            result = results[0] if results else {}
        elif isinstance(results, dict):
            result = results
        else:
            result = {}
        job_id = str(result.get("tracking_id") or result.get("task_id") or tracking_id or "")
        status = str(result.get("status") or "pending").lower()
        complete = status == "success"
        if status in {"failure", "revoked", "not_found", "unknown"}:
            # This provider task cannot make further progress. Clear its id so
            # the next durable erasure retry submits a fresh deletion job.
            return False, "", status
        if not job_id and not complete:
            logger.warning("Mixpanel deletion response had no tracking id for %s", distinct_id)
            return False, "", "malformed_response"
        return complete, job_id, status
    except Exception as exc:
        logger.warning("Mixpanel historical deletion failed for %s: %s", distinct_id, exc)
        return False, tracking_id, "request_failed"
