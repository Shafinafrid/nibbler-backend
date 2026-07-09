"""
Bug-report Google Sheet mirror.

Shafin maintains the monthly files himself (duplicated from a template),
inside Shafin's shared Drive folder (settings.bug_drive_folder_id):

    <root shared folder>/
        2026/
            bug-report-july      ← duplicated from the template by Shafin
            bug-report-september ← months with no file yet are simply skipped

This module only SEARCHES for the year folder and month file by name and
APPENDS a row — it never creates folders or files. That's deliberate:
service accounts have zero Drive storage quota, so file/folder *creation*
via the API can fail even with Editor access on the parent; searching an
existing tree and appending values to an existing spreadsheet the account
has Editor rights on does not touch that limit at all.

Sheet columns: Reported at · User ID (Firebase UID) · Name · Where ·
What happened · Resolved? (checkbox — left unchecked for Shafin to fill).

Auth reuses the SAME service account the backend already uses for Firebase
Admin (settings.firebase_client_email). For this to work Shafin must, once:
  1. share the Drive folder with that service-account email as Editor, and
  2. enable the "Google Drive API" and "Google Sheets API" for the Firebase
     project in Google Cloud Console.
Until then (or on any Google-side failure, or a missing month file) this
module logs and returns False — the Postgres row and the email are the
fallbacks, a report is never lost because of Drive.
"""
import asyncio
import datetime
import logging
from typing import Optional, Tuple

import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/spreadsheets",
]
DRIVE = "https://www.googleapis.com/drive/v3"
SHEETS = "https://sheets.googleapis.com/v4"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]

_credentials = None  # cached google-auth credentials (token auto-refreshed)


def _get_access_token_sync() -> str:
    """Build/refresh service-account credentials (sync — call via to_thread)."""
    global _credentials
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    if _credentials is None:
        info = {
            "type": "service_account",
            "project_id": settings.firebase_project_id,
            "private_key_id": settings.firebase_private_key_id,
            "private_key": settings.firebase_private_key.replace("\\n", "\n"),
            "client_email": settings.firebase_client_email,
            "client_id": settings.firebase_client_id,
            "token_uri": "https://oauth2.googleapis.com/token",
        }
        _credentials = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    if not _credentials.valid:
        _credentials.refresh(Request())
    return _credentials.token


def report_timestamp() -> str:
    """Human-readable report time in Shafin's timezone (Stockholm)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.datetime.now(ZoneInfo("Europe/Stockholm"))
        return now.strftime("%Y-%m-%d %H:%M") + " (Stockholm)"
    except Exception:
        return datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M") + " (UTC)"


async def _find_child(client, headers, parent_id, name, mime) -> Optional[str]:
    q = (f"'{parent_id}' in parents and name = '{name}' "
         f"and mimeType = '{mime}' and trashed = false")
    r = await client.get(f"{DRIVE}/files", headers=headers, params={
        "q": q, "fields": "files(id,name)",
        "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
    })
    r.raise_for_status()
    files = r.json().get("files", [])
    return files[0]["id"] if files else None


async def append_bug_report(
    reported_at: str,
    user_id: str,
    name: str,
    where_seen: str,
    description: str,
) -> Tuple[bool, str]:
    """Append one report row to this month's sheet. Returns (ok, detail) —
    never raises. ok=False (with a human-readable detail) if the year
    folder or month file doesn't exist yet, or Google isn't configured."""
    if not settings.firebase_client_email or not settings.firebase_private_key:
        return False, "service account not configured"

    now = datetime.datetime.now()
    year, month = str(now.year), MONTHS[now.month - 1]
    file_name = f"bug-report-{month}"

    try:
        token = await asyncio.to_thread(_get_access_token_sync)
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=30) as client:
            year_id = await _find_child(client, headers, settings.bug_drive_folder_id, year, FOLDER_MIME)
            if not year_id:
                return False, f"no '{year}' folder yet in the bug-report Drive folder"

            sheet_id = await _find_child(client, headers, year_id, file_name, SHEET_MIME)
            if not sheet_id:
                return False, f"no '{file_name}' sheet yet in the '{year}' folder — duplicate the template and name it exactly that"

            r = await client.post(
                f"{SHEETS}/spreadsheets/{sheet_id}/values/A:F:append",
                headers=headers,
                params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                json={"values": [[reported_at, user_id, name, where_seen, description, False]]},
            )
            r.raise_for_status()
        return True, "ok"
    except httpx.HTTPStatusError as e:
        detail = f"Google API {e.response.status_code}: {e.response.text[:300]}"
        logger.error("Bug-report sheet append failed — %s", detail)
        return False, detail
    except Exception as e:
        logger.error("Bug-report sheet append failed — %s", e)
        return False, str(e)
