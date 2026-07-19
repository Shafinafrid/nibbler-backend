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

# Minimum hours between one user's scheduled nibble sets. A hard ~24h cooldown
# stops users farming extra nibbles by nudging their delivery time forward the
# same day (read at 10:00 → change to 11:00 → new set an hour later). Set to 23h
# (not a strict 24h) so the normal daily cadence isn't blocked by 5-minute-slot
# and generation-time jitter — an unchanged schedule is exactly 24h apart, a
# changed one only re-fires the next day.
NIBBLE_LOCK_HOURS = 23

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


# ── Session-lifecycle helpers (see NIBBLE_SESSION_LIFECYCLE.md) ───────────────
# The scheduler PRE-GENERATES the daily nibble(s) ~5 min before the user's
# delivery time, then notifies at the delivery time. One unread set is held
# until read (never regenerated/obsoleted); a missed cycle resets the streak.

def _slot(dt):
    """(hour, 5-minute-slot) in UTC — matches how delivery times are stored."""
    return dt.hour, (dt.minute // 5) * 5


def _load_growth_state(db, user_id: str) -> dict:
    from app.models.profile import Profile
    prof = db.query(Profile).filter(Profile.user_id == user_id).first()
    return (prof.growth_state if prof and prof.growth_state else {}) or {}


def _pick_profile(growth_state: dict, item):
    """Mirror the app's buildSessionPayload: book-matched profile, else active, else first."""
    profiles = growth_state.get("profiles") or []
    if not profiles:
        return None
    gp = None
    name = getattr(item, "growth_profile_name", None)
    if name:
        gp = next((p for p in profiles if (p.get("profileName") or p.get("name")) == name), None)
    if gp is None:
        active_id = growth_state.get("activeProfileId")
        gp = next((p for p in profiles if p.get("id") == active_id or p.get("profileId") == active_id), None)
    return gp or profiles[0]


def _build_profile_dict(growth_state: dict, item) -> dict:
    """Same shape the app sends in growth_profile (sessionPrefetch.buildSessionPayload)."""
    gp = _pick_profile(growth_state, item)
    if not gp:
        return {}
    interests = [(i.get("tag") if isinstance(i, dict) else i) for i in (gp.get("interests") or [])]
    return {
        "name": gp.get("profileName") or gp.get("name"),
        "lifeArea": gp.get("lifeArea"),
        "aspirationLabel": gp.get("aspirationLabel"),
        "aspirationUnderstanding": gp.get("aspirationUnderstanding"),
        "confidenceStyle": gp.get("confidenceStyle"),
        "goalOrientation": gp.get("goalOrientation"),
        "contentMode": gp.get("contentMode"),
        "interests": [i for i in interests if i],
    }


def _read_length_for(growth_state: dict, item) -> int:
    gp = _pick_profile(growth_state, item)
    dm = (gp.get("pacing") or {}).get("dailyMinutes") if gp else None
    return dm if dm in (5, 10, 15) else 5


def _select_sources_for_today(active: list, count: int, today) -> list:
    """Pick `count` active sources, rotating the window daily so all get airtime.
    Premium cap is 3 even with 5 active — the same 3-wide window shifts each day."""
    n = len(active)
    if n <= count:
        return active[:count]
    offset = today.toordinal() % n
    return [active[(offset + i) % n] for i in range(count)]


def _reset_streak_if_broken(db, user_id: str, today) -> None:
    """Called AT the user's generation boundary (T−5 tick). A full cycle with
    no completed session — regardless of how that cycle's sessions were
    generated (scheduled or manual/prefetched) — resets the streak to 0, so
    the number the app shows is already 0 the moment the miss becomes final."""
    from datetime import datetime, timedelta
    from app.models.streak import Streak
    s = db.query(Streak).filter(Streak.user_id == user_id).first()
    if not s or not s.current_streak:
        return
    if s.last_completed_at:
        broken = s.last_completed_at < datetime.utcnow() - timedelta(hours=24)
    else:
        broken = not s.last_active_date or s.last_active_date < today - timedelta(days=1)
    if broken:
        s.current_streak = 0
        db.commit()
        logger.info("Streak reset (missed cycle) for user %s", user_id)


def _prepare_user_nibbles(db_factory, user_id: str) -> None:
    """
    Blocking (Claude/embeddings) — always call via asyncio.to_thread so it never
    stalls the event loop. Generates today's scheduled nibble set for one user,
    honoring the hold-until-read rule and streak reset.
    """
    from datetime import date, datetime, timedelta
    from sqlalchemy import func
    from app.models.user import User
    from app.models.library import LibraryItem
    from app.models.bite import DailyBite
    from app.config import get_settings
    from app.services.session_service import generate_session_for_item, SessionGenerationError

    settings = get_settings()
    with db_factory() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        today = date.today()  # UTC day at generation (~the user's local delivery time)

        active = (
            db.query(LibraryItem)
            .filter(
                LibraryItem.user_id == user_id,
                LibraryItem.is_active.is_(True),
                LibraryItem.processed.is_(True),
            )
            .order_by(LibraryItem.id.asc())
            .all()
        )
        # This tick IS the user's cycle boundary — settle the streak first,
        # whatever happens with generation below.
        _reset_streak_if_broken(db, user_id, today)

        if not active:
            return  # nothing to generate from

        # Hold-until-read: never generate a new set while one is unread.
        # Origin-agnostic on purpose: an unread manual/prefetched session is
        # still "the current session" and must be held, not buried under a
        # fresh scheduled one.
        unread = (
            db.query(DailyBite)
            .filter(
                DailyBite.user_id == user_id,
                DailyBite.read_at.is_(None),
            )
            .order_by(DailyBite.date.desc())
            .first()
        )
        if unread:
            return  # keep the held session; do not obsolete or regenerate

        # One set per day, whatever its origin: if the user already generated
        # today's session(s) by tapping a book (origin='manual'), the scheduler
        # must not top them up with a second scheduled set.
        made_today = (
            db.query(DailyBite)
            .filter(DailyBite.user_id == user_id, DailyBite.date >= today)
            .first()
        )
        if made_today:
            return

        # 24-hour cooldown: only prepare a new set once ~24h has passed since the
        # last one. Blocks the "nudge my delivery time forward to farm extra
        # nibbles the same day" exploit — a changed time simply takes effect the
        # next day. Also serves as idempotency so an overlapping cron tick can't
        # double-generate. (Uses generated_at, not `date`, so it can't be beaten
        # by crossing midnight either.)
        last_gen = (
            db.query(func.max(DailyBite.generated_at))
            .filter(
                DailyBite.user_id == user_id,
                DailyBite.origin == "scheduled",
            )
            .scalar()
        )
        if last_gen and (datetime.utcnow() - last_gen) < timedelta(hours=NIBBLE_LOCK_HOURS):
            return

        cap = settings.premium_bites_per_day if user.effective_premium else settings.free_bites_per_day
        selected = _select_sources_for_today(active, min(cap, len(active)), today)
        growth_state = _load_growth_state(db, user_id)

        made = 0
        for item in selected:
            try:
                generate_session_for_item(
                    db, user=user, item=item,
                    read_length=_read_length_for(growth_state, item),
                    profile=_build_profile_dict(growth_state, item),
                    today=today, origin="scheduled",
                )
                made += 1
            except SessionGenerationError as e:
                logger.warning("Scheduled gen skipped (user=%s item=%s): %s", user_id, item.id, e.message)
            except Exception as e:
                logger.error("Scheduled gen error (user=%s item=%s): %s", user_id, item.id, e)
        logger.info("Prepared %d scheduled nibble(s) for user %s", made, user_id)


async def _notify_delivery_slot(db_factory, now) -> None:
    """Notify users whose delivery time is `now`, with copy that depends on
    whether their held set is fresh (prepared today) or forgotten (older)."""
    from app.models.push_token import PushToken
    from app.models.bite import DailyBite
    from app.config import get_settings

    settings = get_settings()
    hour, slot = _slot(now)
    today = now.date()

    fresh_tokens, forgotten_tokens = [], []
    with db_factory() as db:
        rows = (
            db.query(PushToken)
            .filter(PushToken.notification_hour == hour, PushToken.notification_minute == slot)
            .all()
        )
        if not rows:
            return
        by_user: dict[str, list[str]] = {}
        for r in rows:
            by_user.setdefault(r.user_id, []).append(r.token)
        for user_id, toks in by_user.items():
            # Origin-agnostic: a session the user generated themselves (manual/
            # prefetched) is still today's session — remind about it the same way.
            unread = (
                db.query(DailyBite)
                .filter(
                    DailyBite.user_id == user_id,
                    DailyBite.read_at.is_(None),
                )
                .order_by(DailyBite.date.desc())
                .first()
            )
            if not unread:
                continue  # nothing prepared (no active sources / gen failed) → no push
            (fresh_tokens if unread.date >= today else forgotten_tokens).extend(toks)

    expo = getattr(settings, "expo_access_token", "")
    if fresh_tokens:
        logger.info("Delivering fresh nibble to %d tokens (%02d:%02d UTC)", len(fresh_tokens), hour, slot)
        await send_push_notifications(
            tokens=fresh_tokens,
            title="Your daily nibble is ready 🐱",
            body="Nibbler prepared something fresh for you — tap to dig in.",
            data={"screen": "Home"},
            expo_access_token=expo,
        )
    if forgotten_tokens:
        logger.info("Reminding %d tokens of a held nibble (%02d:%02d UTC)", len(forgotten_tokens), hour, slot)
        await send_push_notifications(
            tokens=forgotten_tokens,
            title="Psst… you forgot yesterday's nibble 🐱",
            body="No worries — Nibbler kept it warm for you. Tap to finish it.",
            data={"screen": "Home"},
            expo_access_token=expo,
        )


async def _notify_streak_alert_slot(db_factory, now) -> None:
    """Streak alert — fires 65 minutes before a user's delivery time (one hour
    before their generation boundary), the last stretch of the closing window.
    Sent only when it can still make a difference:
      · the user's streak-alert toggle is on (push_tokens.streak_alerts_enabled)
      · they have an unread session from a PRIOR cycle (any origin)
      · their current streak is > 0 (already-broken streaks have nothing to lose)
    """
    from datetime import timedelta
    from app.models.push_token import PushToken
    from app.models.bite import DailyBite
    from app.models.streak import Streak
    from app.config import get_settings

    settings = get_settings()
    hour, slot = _slot(now + timedelta(minutes=65))
    today = now.date()

    at_risk_tokens = []
    with db_factory() as db:
        rows = (
            db.query(PushToken)
            .filter(
                PushToken.notification_hour == hour,
                PushToken.notification_minute == slot,
                PushToken.streak_alerts_enabled.is_(True),
            )
            .all()
        )
        if not rows:
            return
        by_user: dict[str, list[str]] = {}
        for r in rows:
            by_user.setdefault(r.user_id, []).append(r.token)
        for user_id, toks in by_user.items():
            streak = db.query(Streak).filter(Streak.user_id == user_id).first()
            if not streak or not streak.current_streak:
                continue
            if streak.last_active_date and streak.last_active_date >= today:
                continue  # already read today — nothing at risk
            held = (
                db.query(DailyBite)
                .filter(
                    DailyBite.user_id == user_id,
                    DailyBite.read_at.is_(None),
                    DailyBite.date < today,
                )
                .first()
            )
            if not held:
                continue  # no session whose window is closing
            at_risk_tokens.extend(toks)

    if at_risk_tokens:
        logger.info("Streak alert to %d tokens (%02d:%02d UTC delivery)", len(at_risk_tokens), hour, slot)
        await send_push_notifications(
            tokens=at_risk_tokens,
            title="Your streak ends in 1 hour 🔥",
            body="Yesterday's nibble is still waiting — finish it now to keep your streak alive.",
            data={"screen": "Home"},
            expo_access_token=getattr(settings, "expo_access_token", ""),
        )


async def _run_delivery_cycle(db_factory) -> None:
    """
    Runs every 5 minutes. Three jobs in one tick:
      1. STREAK ALERT for users whose delivery is 65 min from now (T−65) and
         whose streak is about to break.
      2. PRE-GENERATE for users whose delivery is 5 min from now (their T−5 slot).
      3. NOTIFY users whose delivery is now.
    Generation is blocking, so it runs in a threadpool to spare the event loop.
    """
    import asyncio
    from datetime import datetime, timezone, timedelta
    from app.models.push_token import PushToken

    now = datetime.now(timezone.utc)

    try:
        await _notify_streak_alert_slot(db_factory, now)
    except Exception as e:
        logger.error("Streak-alert pass failed: %s", e)

    gen_hour, gen_slot = _slot(now + timedelta(minutes=5))
    with db_factory() as db:
        gen_user_ids = [
            uid for (uid,) in db.query(PushToken.user_id)
            .filter(PushToken.notification_hour == gen_hour, PushToken.notification_minute == gen_slot)
            .distinct()
            .all()
        ]
    for uid in gen_user_ids:
        try:
            await asyncio.to_thread(_prepare_user_nibbles, db_factory, uid)
        except Exception as e:
            logger.error("Pre-generation failed for user %s: %s", uid, e)

    await _notify_delivery_slot(db_factory, now)


def start_scheduler(db_factory) -> None:
    """
    Start the APScheduler with the daily notification job.
    db_factory should be a callable that returns a context-managed DB session.
    """
    if scheduler.running:
        return

    scheduler.add_job(
        _run_delivery_cycle,
        trigger="cron",
        minute="*/5",    # every 5 minutes — pre-generate (T−5) + deliver (T)
        kwargs={"db_factory": db_factory},
        id="daily_bite_reminder",
        replace_existing=True,
        misfire_grace_time=240,  # under one slot, so a stalled tick can't double-fire into the next
        max_instances=1,         # never overlap a still-running generation cycle
    )
    scheduler.start()
    logger.info("Notification scheduler started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
