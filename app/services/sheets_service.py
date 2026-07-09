"""
Bug-report Google Sheet mirror.

Writes each bug report into Shafin's shared Drive folder
(settings.bug_drive_folder_id) with this layout:

    <root shared folder>/
        2026/
            bug-report-july      ← created on the first report of that month
            bug-report-september ← months with no reports get no file

Sheet columns: Reported at · User ID (Firebase UID) · Name · Where ·
What happened · Resolved? (one-click checkbox — left unchecked for Shafin).

Auth reuses the SAME service account the backend already uses for Firebase
Admin (settings.firebase_client_email). For this to work Shafin must, once:
  1. share the Drive folder with that service-account email as Editor, and
  2. enable the "Google Drive API" and "Google Sheets API" on the Firebase
     project in Google Cloud Console.
Until then (or on any Google-side failure) this module logs and returns
False — the Postgres row and the email are the fallbacks, a report is
never lost because of Drive.
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
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
DRIVE = "https://www.googleapis.com/drive/v3"
SHEETS = "https://sheets.googleapis.com/v4"
FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]

HEADER = ["Reported at", "User ID", "Name", "Where", "What happened", "Resolved?"]

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


async def _create_folder(client, headers, parent_id, name) -> str:
    r = await client.post(f"{DRIVE}/files", headers=headers,
                          params={"supportsAllDrives": "true"},
                          json={"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]})
    r.raise_for_status()
    return r.json()["id"]


async def _create_month_sheet(client, headers, parent_id, name) -> str:
    """Create the monthly spreadsheet with header row + checkbox column."""
    r = await client.post(f"{DRIVE}/files", headers=headers,
                          params={"supportsAllDrives": "true"},
                          json={"name": name, "mimeType": SHEET_MIME, "parents": [parent_id]})
    r.raise_for_status()
    sheet_id = r.json()["id"]

    # Header text
    r = await client.put(
        f"{SHEETS}/spreadsheets/{sheet_id}/values/A1:F1",
        headers=headers, params={"valueInputOption": "RAW"},
        json={"values": [HEADER]},
    )
    r.raise_for_status()

    # Formatting: bold orange header, frozen row, column widths,
    # one-click checkboxes in the "Resolved?" column.
    requests = [
        {"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "backgroundColor": {"red": 0.91, "green": 0.42, "blue": 0.12},  # Nibbler orange
            }},
            "fields": "userEnteredFormat(textFormat,backgroundColor)",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": 0, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        {"setDataValidation": {
            "range": {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 5000,
                      "startColumnIndex": 5, "endColumnIndex": 6},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True},
        }},
        # Column widths: time 170, uid 240, name 130, where 150, what 420, resolved 90
        *[{"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize",
        }} for i, w in enumerate([170, 240, 130, 150, 420, 90])],
        {"repeatCell": {
            "range": {"sheetId": 0, "startRowIndex": 1, "endRowIndex": 5000,
                      "startColumnIndex": 4, "endColumnIndex": 5},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy",
        }},
    ]
    r = await client.post(f"{SHEETS}/spreadsheets/{sheet_id}:batchUpdate",
                          headers=headers, json={"requests": requests})
    r.raise_for_status()
    return sheet_id


async def append_bug_report(
    reported_at: str,
    user_id: str,
    name: str,
    where_seen: str,
    description: str,
) -> Tuple[bool, str]:
    """Append one report row, creating the year folder / month file as
    needed. Returns (ok, detail) — never raises."""
    if not settings.firebase_client_email or not settings.firebase_private_key:
        return False, "service account not configured"

    try:
        token = await asyncio.to_thread(_get_access_token_sync)
        headers = {"Authorization": f"Bearer {token}"}
        now = datetime.datetime.now()
        year, month = str(now.year), MONTHS[now.month - 1]

        async with httpx.AsyncClient(timeout=30) as client:
            year_id = await _find_child(client, headers, settings.bug_drive_folder_id, year, FOLDER_MIME)
            if not year_id:
                year_id = await _create_folder(client, headers, settings.bug_drive_folder_id, year)

            file_name = f"bug-report-{month}"
            sheet_id = await _find_child(client, headers, year_id, file_name, SHEET_MIME)
            if not sheet_id:
                sheet_id = await _create_month_sheet(client, headers, year_id, file_name)

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
