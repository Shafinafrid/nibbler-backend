"""
Account-deletion Google Sheet mirror (Task 7, Aug 2026).

Two tabs on the same spreadsheet (settings.account_deletion_sheet_id):
  "Deletion Log"  — one row per deletion job, ops-focused: status, retry
                    count, per-artifact-class booleans. Updated repeatedly
                    as the job progresses (pending -> retrying -> complete).
  "User Snapshot" — one row per deletion job, informational: what the
                    account looked like right before it was erased (plan,
                    device, growth-profile count, engagement stats). This
                    is deliberately METADATA ONLY — no note/highlight/chat
                    text content — this sheet stays privacy-safe by design,
                    not just by naming.

Postgres (AccountErasure) is the ONLY source of truth. Every function here
is best-effort and NEVER raises — the same reliability contract as
sheets_service.py's bug-report mirror: a Sheet outage must never block or
fail the actual account-deletion job. Gated by
settings.account_deletion_automation_enabled so this can ship inert and be
verified (a non-destructive connection test, then a disposable-account
end-to-end run) before going live.

Auth reuses the SAME service account as sheets_service.py / Firebase Admin
(settings.firebase_client_email) — no new credential, no new Railway var.

This module is SYNCHRONOUS (httpx.Client, not AsyncClient) because its one
call site, app/routers/auth.py's _attempt_account_erasure_cleanup, is a sync
function shared by the sync DELETE /auth/me route and the sync autonomous
retry worker (entitlement_service.retry_account_erasures) — neither runs
inside an event loop.
"""
import datetime
import logging
import threading
from typing import Optional

import httpx
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SHEETS = "https://sheets.googleapis.com/v4"

DELETION_LOG_COLUMNS = [
    "Deletion Request ID", "Requested At", "User Email", "Firebase UID",
    "Plan", "Status", "Deletion Started At", "Deletion Completed At",
    "PostgreSQL Deleted", "Firebase Deleted", "Uploaded Files Deleted",
    "Extracted Images Deleted", "Search Vectors Deleted",
    "Analytics Data Deleted", "RevenueCat Data Handled",
    "Confirmation Email Sent", "Retry Count", "Failure Details",
    "Manual Action Required", "Personal Data Redacted At",
]
_DELETION_LOG_LAST_COL = "T"  # len(DELETION_LOG_COLUMNS) == 20

SNAPSHOT_COLUMNS = [
    "Deletion Request ID", "Requested At", "User Email", "Firebase UID",
    "Account Created At", "Plan At Deletion", "Premium Until",
    "Platform", "App Version", "Device Model", "OS Version",
    "Timezone", "Locale",
    "Growth Profile Count", "Primary Life Area", "Primary Content Mode",
    "Library Item Count", "Total Uploaded MB", "Total Pinecone Vectors",
    "Current Streak", "Longest Streak", "Total Bites Read",
    "Notes Count", "Highlights Count", "Chat Messages Count", "Completions Count",
    "RevenueCat Product ID", "RevenueCat Original Purchase Date", "RevenueCat Store",
]
_SNAPSHOT_LAST_COL = "AC"  # len(SNAPSHOT_COLUMNS) == 29

_credentials = None  # cached google-auth credentials (token auto-refreshed)


def _get_access_token_sync() -> Optional[str]:
    if not settings.firebase_client_email or not settings.firebase_private_key:
        return None
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


def _col_letter(index: int) -> str:
    """0-based column index -> A1 letter(s)."""
    letters = ""
    n = index + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def _get_or_create_tab(client: httpx.Client, headers: dict, sheet_id: str, tab_name: str, header_row: list) -> bool:
    """Returns True if the tab exists (or was created) with a header row in place."""
    r = client.get(f"{SHEETS}/spreadsheets/{sheet_id}",
                    headers=headers, params={"fields": "sheets.properties(title,sheetId)"})
    r.raise_for_status()
    titles = [s["properties"]["title"] for s in r.json().get("sheets", [])]
    if tab_name in titles:
        return True
    # Create it with a frozen header row, then write the header.
    r = client.post(f"{SHEETS}/spreadsheets/{sheet_id}:batchUpdate", headers=headers, json={
        "requests": [{"addSheet": {"properties": {
            "title": tab_name, "gridProperties": {"rowCount": 1000, "frozenRowCount": 1},
        }}}]
    })
    r.raise_for_status()
    last_col = _col_letter(len(header_row) - 1)
    r = client.put(
        f"{SHEETS}/spreadsheets/{sheet_id}/values/'{tab_name}'!A1:{last_col}1",
        # RAW, not USER_ENTERED: every value written here originates from
        # account data (email, device strings, etc.) — USER_ENTERED lets
        # Sheets interpret a leading =/+/-/@ as a formula, the exact
        # formula-injection vector this task's own requirement calls out.
        # RAW stores the literal string/number with no interpretation.
        headers=headers, params={"valueInputOption": "RAW"},
        json={"values": [header_row]},
    )
    r.raise_for_status()
    return True


def _next_empty_row(client: httpx.Client, headers: dict, sheet_id: str, tab_name: str) -> int:
    r = client.get(f"{SHEETS}/spreadsheets/{sheet_id}/values/'{tab_name}'!A:A", headers=headers)
    r.raise_for_status()
    rows = r.json().get("values", [])
    return max(len(rows) + 1, 2)  # never below row 2 (row 1 is the header)


def _ensure_grid_rows(client: httpx.Client, headers: dict, sheet_id: str, tab_name: str, needed_row: int):
    r = client.get(f"{SHEETS}/spreadsheets/{sheet_id}", headers=headers,
                    params={"fields": "sheets.properties(title,sheetId,gridProperties)"})
    r.raise_for_status()
    tab = next((s["properties"] for s in r.json()["sheets"] if s["properties"]["title"] == tab_name), None)
    if tab is None:
        return
    row_count = tab.get("gridProperties", {}).get("rowCount", 0)
    if needed_row <= row_count:
        return
    r = client.post(f"{SHEETS}/spreadsheets/{sheet_id}:batchUpdate", headers=headers, json={
        "requests": [{"appendDimension": {
            "sheetId": tab["sheetId"], "dimension": "ROWS",
            "length": needed_row - row_count + 200,
        }}]
    })
    r.raise_for_status()


def _write_row(client: httpx.Client, headers: dict, sheet_id: str, tab_name: str,
                row: int, last_col: str, values: list) -> None:
    _ensure_grid_rows(client, headers, sheet_id, tab_name, row)
    r = client.put(
        f"{SHEETS}/spreadsheets/{sheet_id}/values/'{tab_name}'!A{row}:{last_col}{row}",
        # RAW, not USER_ENTERED: every value written here originates from
        # account data (email, device strings, etc.) — USER_ENTERED lets
        # Sheets interpret a leading =/+/-/@ as a formula, the exact
        # formula-injection vector this task's own requirement calls out.
        # RAW stores the literal string/number with no interpretation.
        headers=headers, params={"valueInputOption": "RAW"},
        json={"values": [values]},
    )
    r.raise_for_status()


def deletion_log_row_values(erasure, snapshot: dict) -> list:
    """Build the Deletion Log row from current DB state — always the full
    row, positionally, so a re-sync after a retry just overwrites in place.

    Founder decision (Aug 2026): email/Firebase UID are kept permanently
    for support lookups, NOT redacted once personal_data_redacted_at is
    set — that field still records when cleanup completed, it just no
    longer blanks anything here."""
    progress = erasure.progress or {}
    return [
        erasure.id,
        (erasure.requested_at.isoformat() if erasure.requested_at else ""),
        snapshot.get("email", ""),
        snapshot.get("firebase_uid", ""),
        snapshot.get("plan", ""),
        erasure.state,
        (erasure.deletion_started_at.isoformat() if erasure.deletion_started_at else ""),
        (erasure.deletion_completed_at.isoformat() if erasure.deletion_completed_at else ""),
        # Audit finding: this must mean what its header says — the user row
        # is ACTUALLY gone — not a partial AND of unrelated classes that
        # could be True while Firebase/RevenueCat/Mixpanel/vectors are
        # still failing. erasure.state == "resolved" is set ONLY in the
        # all-classes-succeeded branch, right before the row is deleted.
        erasure.state == "resolved",
        bool(progress.get("firebase")),
        bool(progress.get("s3_files")),
        bool(progress.get("s3_images")),
        bool(progress.get("vectors")),
        bool(progress.get("mixpanel")),
        bool(progress.get("revenuecat")),
        bool(progress.get("email_sent")),
        erasure.retry_count or 0,
        ", ".join(k for k, v in progress.items() if not v) if progress else "",
        "yes" if (erasure.state == "failed" and (erasure.retry_count or 0) >= 5) else "",
        (erasure.personal_data_redacted_at.isoformat() if erasure.personal_data_redacted_at else ""),
    ]


def _short(value, limit=60) -> str:
    """Audit finding: primary_life_area/primary_content_mode come from
    growth_state, an unvalidated client-controlled JSON blob (PUT
    /profile/growth has no field-level checks) — a buggy or unusual client
    could in principle put arbitrary long free text there. Truncating here
    keeps the snapshot's own metadata-only, non-free-text guarantee true
    regardless of what Postgres happens to hold, rather than trusting the
    upstream shape."""
    s = str(value or "")
    return s[:limit] + "…" if len(s) > limit else s


def snapshot_row_values(erasure, snapshot: dict) -> list:
    # Founder decision (Aug 2026): email/Firebase UID kept permanently for
    # support lookups — see deletion_log_row_values' docstring above.
    return [
        erasure.id,
        (erasure.requested_at.isoformat() if erasure.requested_at else ""),
        snapshot.get("email", ""),
        snapshot.get("firebase_uid", ""),
        snapshot.get("created_at_iso", ""),
        snapshot.get("plan", ""),
        snapshot.get("premium_until_iso", ""),
        snapshot.get("platform", ""),
        snapshot.get("app_version", ""),
        snapshot.get("device_model", ""),
        snapshot.get("os_version", ""),
        snapshot.get("timezone", ""),
        snapshot.get("locale", ""),
        snapshot.get("growth_profile_count", 0),
        _short(snapshot.get("primary_life_area", "")),
        _short(snapshot.get("primary_content_mode", "")),
        snapshot.get("library_item_count", 0),
        snapshot.get("library_total_mb", 0),
        snapshot.get("total_vectors", 0),
        snapshot.get("current_streak", 0),
        snapshot.get("longest_streak", 0),
        snapshot.get("total_bites_read", 0),
        snapshot.get("notes_count", 0),
        snapshot.get("highlights_count", 0),
        snapshot.get("chat_messages_count", 0),
        snapshot.get("completions_count", 0),
        snapshot.get("rc_product_id", ""),
        snapshot.get("rc_original_purchase_date", ""),
        snapshot.get("rc_store", ""),
    ]


# Audit finding: this project runs uvicorn single-process (documented
# constraint — see nibbler-backend/CLAUDE.md, "do NOT run multiple
# workers"), but WITHIN that one process a live DELETE /auth/me request
# (FastAPI's sync threadpool) and the autonomous retry worker
# (entitlement_service.retry_account_erasures, via asyncio.to_thread) can
# both be allocating a brand-new sheet row for two DIFFERENT erasures at
# the same instant — _next_empty_row's read-then-later-write has no
# atomicity of its own. A plain process-local lock is sufficient here
# (not a Postgres advisory lock) because the single-process constraint is
# already load-bearing elsewhere in this codebase.
_row_allocation_lock = threading.Lock()


def _allocate_sheet_rows(client, headers, sheet_id, erasure) -> None:
    """Ensure both tabs exist and this erasure has row numbers assigned —
    mutates erasure.sheet_row/snapshot_sheet_row in place. Locked so two
    different erasures' first-ever sync can't race on _next_empty_row."""
    with _row_allocation_lock:
        _get_or_create_tab(client, headers, sheet_id, settings.account_deletion_sheet_tab, DELETION_LOG_COLUMNS)
        if erasure.sheet_row is None:
            erasure.sheet_row = _next_empty_row(client, headers, sheet_id, settings.account_deletion_sheet_tab)

        _get_or_create_tab(client, headers, sheet_id, settings.account_deletion_snapshot_tab, SNAPSHOT_COLUMNS)
        if erasure.snapshot_sheet_row is None:
            erasure.snapshot_sheet_row = _next_empty_row(client, headers, sheet_id, settings.account_deletion_snapshot_tab)


def sync_erasure_to_sheet(erasure, snapshot: dict) -> bool:
    """Create-or-update this job's row on BOTH tabs. Returns True only if
    both writes succeeded. Never raises. No-op (returns False) if the
    feature flag is off or credentials aren't configured — deliberately
    silent in that case, since the flag being off is expected during
    rollout, not an error worth logging every single erasure attempt.

    Safe to call for an in-progress or failed erasure (the row still
    exists in Postgres either way). For the FULL-SUCCESS path, where the
    erasure row is about to be deleted, use prepare_success_write() /
    write_prepared_rows() instead — see their docstrings for why."""
    if not settings.account_deletion_automation_enabled:
        return False
    token = _get_access_token_sync()
    if not token:
        logger.warning("Deletion-sheet sync skipped for %s: service account not configured", erasure.id)
        return False

    headers = {"Authorization": f"Bearer {token}"}
    sheet_id = settings.account_deletion_sheet_id
    try:
        with httpx.Client(timeout=20) as client:
            _allocate_sheet_rows(client, headers, sheet_id, erasure)
            _write_row(client, headers, sheet_id, settings.account_deletion_sheet_tab,
                       erasure.sheet_row, _DELETION_LOG_LAST_COL, deletion_log_row_values(erasure, snapshot))
            _write_row(client, headers, sheet_id, settings.account_deletion_snapshot_tab,
                       erasure.snapshot_sheet_row, _SNAPSHOT_LAST_COL, snapshot_row_values(erasure, snapshot))
    except httpx.HTTPStatusError as e:
        logger.error("Deletion-sheet sync failed for %s: Google API %s: %s",
                     erasure.id, e.response.status_code, e.response.text[:300])
        return False
    except Exception as e:
        logger.error("Deletion-sheet sync failed for %s: %s", erasure.id, e)
        return False
    return True


def prepare_success_write(erasure, snapshot: dict):
    """Audit finding: on full erasure success, the erasure ROW gets
    deleted from Postgres in the same commit — writing to the Sheet
    BEFORE that commit risked the Sheet permanently showing 'resolved'
    for a job that then failed to commit (or, worse, one that later hits
    an unrelated bug and never actually finishes). This freezes
    everything the Sheet write needs (row VALUES, computed from current
    in-memory state, and row NUMBERS, allocated via a real Sheets API
    call — safe to do before commit since it doesn't touch Postgres) so
    the actual network WRITE can happen strictly after db.commit()
    succeeds, using write_prepared_rows() below — Postgres becomes
    authoritative first, the Sheet mirrors it only once that's certain.

    Returns None (and logs) if unconfigured/disabled/allocation fails —
    caller should treat that as 'skip the write', not an error."""
    if not settings.account_deletion_automation_enabled:
        return None
    token = _get_access_token_sync()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    sheet_id = settings.account_deletion_sheet_id
    try:
        with httpx.Client(timeout=20) as client:
            _allocate_sheet_rows(client, headers, sheet_id, erasure)
        return {
            "sheet_row": erasure.sheet_row,
            "snapshot_sheet_row": erasure.snapshot_sheet_row,
            "deletion_log_values": deletion_log_row_values(erasure, snapshot),
            "snapshot_values": snapshot_row_values(erasure, snapshot),
        }
    except Exception as e:
        logger.warning("Deletion-sheet row allocation failed for %s: %s", erasure.id, e)
        return None


def write_prepared_rows(prepared: dict) -> bool:
    """The actual network write for prepare_success_write()'s output —
    call ONLY after the Postgres commit that deletes the erasure row has
    already succeeded. Never raises."""
    if not prepared:
        return False
    token = _get_access_token_sync()
    if not token:
        return False
    headers = {"Authorization": f"Bearer {token}"}
    sheet_id = settings.account_deletion_sheet_id
    try:
        with httpx.Client(timeout=20) as client:
            _write_row(client, headers, sheet_id, settings.account_deletion_sheet_tab,
                       prepared["sheet_row"], _DELETION_LOG_LAST_COL, prepared["deletion_log_values"])
            _write_row(client, headers, sheet_id, settings.account_deletion_snapshot_tab,
                       prepared["snapshot_sheet_row"], _SNAPSHOT_LAST_COL, prepared["snapshot_values"])
    except Exception as e:
        logger.warning("Deletion-sheet write (post-commit) failed: %s", e)
        return False
    return True


def connection_test(write_marker: bool = False) -> tuple:
    """Non-destructive by default: reads spreadsheet metadata and the
    Deletion Log header row. Pass write_marker=True to additionally append
    ONE clearly-labelled 'CONNECTION-TEST' row (caller is responsible for
    getting explicit approval before doing that, and for removing the row
    afterward). Returns (ok, detail)."""
    token = _get_access_token_sync()
    if not token:
        return False, "service account not configured"
    headers = {"Authorization": f"Bearer {token}"}
    sheet_id = settings.account_deletion_sheet_id
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{SHEETS}/spreadsheets/{sheet_id}", headers=headers,
                            params={"fields": "properties.title,sheets.properties(title)"})
            r.raise_for_status()
            meta = r.json()
            titles = [s["properties"]["title"] for s in meta["sheets"]]
            detail = f"title={meta['properties']['title']!r} tabs={titles}"
            if not write_marker:
                return True, detail
            row = _next_empty_row(client, headers, sheet_id, settings.account_deletion_sheet_tab)
            marker = ["CONNECTION-TEST", datetime.datetime.utcnow().isoformat(), "", "", "", "test", *([""] * 14)]
            _write_row(client, headers, sheet_id, settings.account_deletion_sheet_tab,
                       row, _DELETION_LOG_LAST_COL, marker)
            return True, f"{detail} wrote row {row}"
    except httpx.HTTPStatusError as e:
        return False, f"Google API {e.response.status_code}: {e.response.text[:300]}"
    except Exception as e:
        return False, str(e)
