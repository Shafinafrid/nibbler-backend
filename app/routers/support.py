"""
Support — in-app bug reports.

  POST /support/bug-report → 1) row in Postgres (source of truth),
                             2) row in the monthly Google Sheet in Drive,
                             3) notification email to bug-report@getnibbler.com.
  Steps 2 and 3 are best-effort mirrors; the report succeeds if step 1 does.

(The in-app "Contact us" form does NOT go through this backend — it posts
straight to the website's existing /api/contact Vercel function, which
already routes topics to the right inbox and sends the auto-reply.)
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from pydantic import BaseModel
from html import escape

from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.bug_report import BugReport
from app.services import mixpanel_service, sheets_service
from app.services.email_service import send_email
from app.config import get_settings

router = APIRouter(prefix="/support", tags=["support"])
settings = get_settings()


class BugReportRequest(BaseModel):
    where_seen: str
    description: str
    name: Optional[str] = None  # user's name from the app's growth state


class BugReportResponse(BaseModel):
    ok: bool
    sheet: bool
    email: bool
    sheet_detail: Optional[str] = None  # why the sheet write failed, if it did


@router.post("/bug-report", response_model=BugReportResponse)
async def create_bug_report(
    data: BugReportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    where = (data.where_seen or "").strip()[:200]
    description = (data.description or "").strip()[:5000]
    if not where or not description:
        raise HTTPException(status_code=422, detail="Please fill in both fields.")

    name = (data.name or "").strip() or current_user.display_name or (current_user.email or "").split("@")[0]
    reported_at = sheets_service.report_timestamp()

    # 1) Postgres — the report is saved no matter what happens below.
    report = BugReport(
        user_id=current_user.id,
        name=name,
        where_seen=where,
        description=description,
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    # 2) Google Sheet (bug-report-<month> in the year folder)
    sheet_ok, sheet_detail = await sheets_service.append_bug_report(
        reported_at=reported_at,
        user_id=current_user.id,
        name=name,
        where_seen=where,
        description=description,
    )

    # 3) Email to bug-report@getnibbler.com
    s_name, s_where, s_desc = escape(name), escape(where), escape(description)
    email_ok = await send_email(
        to=settings.bug_report_email,
        subject=f"🐛 Bug report: {where} — from {name}",
        from_name="Nibbler Bug Reports",
        reply_to=current_user.email,
        html=(
            '<div style="font-family:Geist,Arial,sans-serif;color:#3A2A1E;max-width:560px">'
            '<h2 style="font-family:Fredoka,Arial,sans-serif;color:#E8620A;margin:0 0 16px">New in-app bug report</h2>'
            '<table style="border-collapse:collapse;width:100%;font-size:15px">'
            f'<tr><td style="padding:6px 10px;color:#7A6A5C;width:120px">When</td><td style="padding:6px 10px">{escape(reported_at)}</td></tr>'
            f'<tr><td style="padding:6px 10px;color:#7A6A5C">User</td><td style="padding:6px 10px"><strong>{s_name}</strong> ({escape(current_user.email or "")})</td></tr>'
            f'<tr><td style="padding:6px 10px;color:#7A6A5C">Firebase UID</td><td style="padding:6px 10px;font-family:monospace;font-size:13px">{escape(current_user.id)}</td></tr>'
            f'<tr><td style="padding:6px 10px;color:#7A6A5C">Where</td><td style="padding:6px 10px">{s_where}</td></tr>'
            "</table>"
            '<div style="margin:18px 0 0;padding:16px;background:#FFF8EF;border:1px solid #EDE5DA;border-radius:12px;white-space:pre-wrap;font-size:15px;line-height:1.6">'
            f"{s_desc}</div>"
            f'<p style="color:#A8998A;font-size:12px;margin-top:18px">Logged to Google Sheets: {"yes" if sheet_ok else "NO — " + escape(sheet_detail)}</p>'
            "</div>"
        ),
        text=(
            f"New in-app bug report\n\nWhen: {reported_at}\nUser: {name} ({current_user.email})\n"
            f"Firebase UID: {current_user.id}\nWhere: {where}\n\n{description}\n\n"
            f"Logged to Google Sheets: {'yes' if sheet_ok else 'NO — ' + sheet_detail}"
        ),
    )

    report.synced_to_sheet = sheet_ok
    report.emailed = email_ok
    db.commit()

    await mixpanel_service.track("bug_report_submitted", current_user.id, {
        "where": where, "sheet": sheet_ok, "email": email_ok,
    })
    return BugReportResponse(
        ok=True, sheet=sheet_ok, email=email_ok,
        sheet_detail=None if sheet_ok else sheet_detail,
    )
