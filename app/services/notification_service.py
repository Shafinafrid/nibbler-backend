"""
Push notification service.

Sends notifications via Expo's push API, which routes to APNs (iOS) and FCM (Android).
No native credentials needed on our end — Expo handles the APNs/FCM routing.

Expo Push API docs: https://docs.expo.dev/push-notifications/sending-notifications/
"""

import hashlib
import logging
from datetime import timezone as dt_timezone
from typing import Optional
from zoneinfo import ZoneInfo
import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"

# Minimum hours between one user's scheduled nibble sets. A hard ~24h cooldown
# stops users farming extra nibbles by nudging their delivery time forward the
# same day (read at 10:00 → change to 11:00 → new set an hour later). Set to 23h
# (not a strict 24h) so the normal daily cadence isn't blocked by 5-minute-slot
# and generation-time jitter — an unchanged schedule is exactly 24h apart, a
# changed one only re-fires the next day.
NIBBLE_LOCK_HOURS = 23

# ── Push copy — Task 3 (Aug 2026): dynamic, book-specific notifications ──────
# The body is `DailyBite.headline` — the SAME "one arresting sentence that
# makes the user want to read" the LLM already produces as part of the normal
# session-generation call (see llm/schemas.py / prompts.py), already required
# by validate_wisdom/validate_story, and already shown on Home/Notebook as
# today's card teaser. Reusing it means a genuinely dynamic, spoiler-free,
# per-book hook with NO second AI request — exactly what the audit asked for.
# `_HOOK_FALLBACKS` stands in only for a bite with no headline (a pre-Task-3
# legacy row) — varied and book-specific, never one fixed message.
_HOOK_FALLBACKS = [
    "Today's page from {title} is ready — a fresh idea, five minutes long.",
    "{title} has something new waiting for you.",
    "One more idea from {title}, picked out just for today.",
    "Your next bite of {title} is ready when you are.",
]


def _fallback_hook(item_title: str, bite_id: str) -> str:
    """Deterministic per-bite pick — the SAME bite always gets the SAME
    fallback line on every reminder about it, without needing to store one."""
    idx = int(hashlib.sha256(bite_id.encode()).hexdigest(), 16) % len(_HOOK_FALLBACKS)
    return _HOOK_FALLBACKS[idx].format(title=item_title)


def _hook_for(item_title: str, bite) -> str:
    hook = (bite.headline or "").strip()
    return hook if hook else _fallback_hook(item_title, bite.id)


# Varied reminder framing for a bite that's been unread across multiple
# cycles (Task 3 item 12: "send VARIED reminders for the same unread
# nibble... using stored hook data and deterministic templates"). Picked by
# how many days it's been held, not randomly, so the SAME day-offset always
# produces the SAME prefix (reproducible/testable) while consecutive days
# genuinely read differently instead of repeating one fixed reminder line.
_REMINDER_PREFIXES = [
    "Still here waiting for you —",
    "Nibbler's holding your spot —",
    "No rush, but it's still there —",
    "Whenever you're ready —",
]


def build_notification_copy(kind: str, item_title: str, bite, today=None) -> tuple[str, str]:
    """(title, body) for one specific featured bite. `kind` is 'fresh' (the
    normal daily push), 'forgotten' (a reminder about a still-unread bite
    from a prior cycle — same stored hook, no regeneration) or 'streak' (the
    T-65 urgency push). Title is always the book itself — the whole point of
    Task 3 is that the notification is ABOUT a specific book, not app
    branding — with a short framing suffix so the three kinds still read
    differently from each other and, for a repeatedly-forgotten bite, from
    day to day too."""
    hook = _hook_for(item_title, bite)
    if kind == "streak":
        return (f"{item_title} — streak ends in 1 hour 🔥", hook)
    if kind == "forgotten":
        days_held = max(0, (today - bite.date).days) if today else 0
        prefix = _REMINDER_PREFIXES[days_held % len(_REMINDER_PREFIXES)]
        return (item_title, f"{prefix} {hook}")
    return (item_title, hook)


def _notification_data(item_id: str, bite) -> dict:
    """Task 3 item 7: the exact nibble AND source id, so a tap can open the
    precise nibble directly instead of guessing "whatever is unread"."""
    return {"screen": "Session", "bookId": item_id, "biteId": bite.id}


def _feature_bite(db: Session, user, candidates: list, today) -> tuple:
    """Among several unread bites (a Premium account can have up to 3 active
    sources' worth prepared the same cycle), pick ONE to feature in the
    push — Task 3 item 14: "feature one rotating nibble ... while keeping
    the others available normally inside the app." The pick is a pure
    function of (the set of eligible book ids, today's date) — no new
    persisted rotation state, the same date-derived-fairness approach
    `_select_sources_for_today` already uses for scheduled generation, so
    which book gets featured shifts day to day rather than always being
    the same one. Returns (bite, item_title, item_id) or (None, None, None)."""
    from app.models.library import LibraryItem
    if not candidates:
        return (None, None, None)
    by_item = {b.library_item_id: b for b in candidates if b.library_item_id}
    if not by_item:
        return (None, None, None)
    ordered_ids = sorted(by_item.keys())
    chosen_id = ordered_ids[today.toordinal() % len(ordered_ids)]
    bite = by_item[chosen_id]
    item = db.query(LibraryItem).filter(LibraryItem.id == chosen_id, LibraryItem.user_id == user.id).first()
    if not item:
        return (None, None, None)
    return (bite, item.title, item.id)


# APScheduler instance (started in main.py lifespan)
scheduler = AsyncIOScheduler()


async def send_push_messages(messages: list[dict], expo_access_token: str = "") -> list[dict]:
    """Low-level: send already-built, independent Expo message dicts (each
    with its own `to`/title/body/data) — chunked 100/request, per Expo's
    limit. Task 3 needs this: once notification copy is dynamic and
    book-specific PER USER, a batch of pushes can no longer share one
    title/body the way the old flat-copy sends did, but Expo's endpoint
    itself already accepts a heterogeneous array in one call, so this is
    still one HTTP round trip per 100 sends, just no longer uniform
    content within it."""
    if not messages:
        return []

    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if expo_access_token:
        headers["Authorization"] = f"Bearer {expo_access_token}"

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


async def send_push_notifications(
    tokens: list[str],
    title: str,
    body: str,
    data: Optional[dict] = None,
    expo_access_token: str = "",
) -> list[dict]:
    """The SAME title/body/data to every token in `tokens` — correct when
    they all belong to ONE user's own devices (the on-demand test button)
    or, historically, when copy was uniform across users. Delegates to
    send_push_messages for the actual chunked HTTP call."""
    if not tokens:
        return []
    messages = [
        {"to": token, "title": title, "body": body, "sound": "default", "data": data or {}}
        for token in tokens
    ]
    return await send_push_messages(messages, expo_access_token)


# ── Session-lifecycle helpers (see NIBBLE_SESSION_LIFECYCLE.md) ───────────────
# The scheduler PRE-GENERATES the daily nibble(s) ~5 min before the user's
# delivery time, then notifies at the delivery time. One unread set is held
# until read (never regenerated/obsoleted); a missed cycle resets the streak.

def _slot(dt):
    """(hour, 5-minute-slot) in UTC — matches how delivery times are stored."""
    return dt.hour, (dt.minute // 5) * 5


def _effective_utc_slot(local_hour, local_minute, tz_name, fallback_hour, fallback_minute, now_utc) -> tuple:
    """Task 4 (items 13/14 — timezone-safe delivery, no frozen UTC value):
    recompute TODAY's UTC delivery slot from the user's stored LOCAL
    wall-clock pick + their CURRENT `users.timezone` (already kept fresh by
    `PATCH /sync/identity` on every launch — see app/routers/bites.py for the
    same ZoneInfo pattern). Because this runs fresh on every 5-minute tick,
    a DST shift or a trip to a new timezone reconciles automatically on the
    very next tick — nothing needs to notice the change or write a
    correction. Falls back to the cached UTC columns (pre-Task-4 behavior,
    unaffected) when there's no local time on record yet (legacy token) or
    the timezone is missing/unrecognized."""
    if local_hour is None or not tz_name:
        return fallback_hour, fallback_minute
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        return fallback_hour, fallback_minute
    local_now = now_utc.astimezone(tz)
    local_target = local_now.replace(
        hour=local_hour, minute=local_minute or 0, second=0, microsecond=0
    )
    utc_target = local_target.astimezone(dt_timezone.utc)
    return utc_target.hour, (utc_target.minute // 5) * 5


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


def _live_unread_query(db: Session, user) -> list:
    """Unread nibbles the user can actually still open, newest first.

    `daily_bites.library_item_id` is a plain nullable String with NO foreign key
    (app/models/bite.py), and DELETE /library/{id} removes only the item, its
    Pinecone vectors and its S3 object — so deleting a book leaves its nibbles
    behind as orphans. An unread orphan is **unreachable**: the Library row it
    belongs to is gone, so nothing in the app can ever open it and POST
    /bites/{id}/read. It therefore satisfies a bare `read_at IS NULL` check
    forever, and all three checks in this module read off that:

      · generation is held permanently → daily nibbles silently stop, with no
        error and no way for the user to clear it;
      · the delivery push says "you forgot yesterday's nibble" every day;
      · the streak alert says "your streak ends in 1 hour" every day.

    Joining to library_items is what makes "unread" mean "unread AND still
    openable". Deactivated books are deliberately NOT excluded — the user can
    still open those from the Library, so their unread nibble legitimately
    holds. (Found 2026-07-25 in an external audit of DATA_PERSISTENCE_WALKTHROUGH.)

    Task 2 remediation: a source that is currently LOCKED is exactly the
    same kind of unreachable as a deleted one — the user cannot open Session/
    Review/Connect for it while locked, so its unread bite can never be
    marked read through the normal flow either. Without excluding it here, a
    single session generated before a downgrade would hold generation,
    the "forgot yesterday's nibble" push, and the streak alert hostage
    FOREVER, the identical failure mode the join above already fixed for
    deleted books. Filtered in Python (not SQL) because lock state depends
    on `user.effective_premium`, a computed property, not a plain column.
    """
    from app.models.bite import DailyBite
    from app.models.library import LibraryItem
    from app.services.entitlement_service import is_source_unlocked

    # An inner join also drops rows whose library_item_id is NULL, which is
    # correct: those are legacy pre-session bites, equally unopenable today.
    rows = (
        db.query(DailyBite, LibraryItem)
        .join(
            LibraryItem,
            (LibraryItem.id == DailyBite.library_item_id)
            & (LibraryItem.user_id == DailyBite.user_id),
        )
        .filter(
            DailyBite.user_id == user.id,
            DailyBite.read_at.is_(None),
        )
        .order_by(DailyBite.date.desc())
        .all()
    )
    return [bite for bite, item in rows if is_source_unlocked(user, item)]


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
    from app.services.entitlement_service import reconcile_free_lock_state

    settings = get_settings()
    with db_factory() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return
        today = date.today()  # UTC day at generation (~the user's local delivery time)

        # The scheduler never goes through get_current_user (no HTTP request
        # is involved), so it is the ONE path that must reconcile the Free
        # lock selection itself before trusting `is_active` below — otherwise
        # a user who downgrades and never reopens the app would keep every
        # source `is_active=True` forever, and the scheduler would keep
        # generating/pushing for locked sources indefinitely (Task 2).
        reconcile_free_lock_state(db, user)

        active = (
            db.query(LibraryItem)
            .filter(
                LibraryItem.user_id == user_id,
                LibraryItem.is_active.is_(True),
                LibraryItem.processed.is_(True),
                LibraryItem.deletion_state.is_(None),  # Task 2 closeout: scheduled generation must never select a tombstoned item
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
        # fresh scheduled one. (Already lock-filtered — see
        # _live_unread_query's docstring.)
        unread_list = _live_unread_query(db, user)
        if unread_list:
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


def preview_notification_for_user(db: Session, user, now) -> tuple[str, str, dict]:
    """The exact (title, body, data) this user's real state would produce
    right now, reusing the SAME selection logic as the real scheduled
    pushes (`_notify_delivery_slot`/`_notify_streak_alert_slot`) — just
    without the delivery-time-slot gate, so it can fire on demand instead
    of waiting for a real tick.

    This is a TEST-ONLY entry point (see POST /notifications/send-test) —
    it still sends through the real `send_push_notifications` call, so
    what arrives on-device is byte-identical to what the real scheduler
    would have sent for this exact state, not a client-side mockup that
    can drift. Raises SessionGenerationError-free ValueError when there is
    genuinely nothing to preview (no unread bite at all — an idle/fresh
    account with nothing generated yet); the caller surfaces this as a
    clear "nothing to send yet" rather than fabricating a fake example
    with no real book or bite behind it.

    Streak is checked BEFORE forgotten-nibble, not after: in the real
    scheduler these are two independent ticks at two different times of day,
    so the same underlying state (an unread bite from a prior day, streak
    still alive) legitimately produces BOTH pushes on different ticks. This
    function only returns one, and `streak_at_risk` is a strict SUBSET of
    the forgotten condition (it additionally requires a live streak) — so
    checking forgotten first would make the streak variant unreachable
    through this button whenever it's actually true, exactly the case worth
    previewing."""
    from app.models.streak import Streak
    from app.services.entitlement_service import reconcile_free_lock_state

    today = now.date()
    reconcile_free_lock_state(db, user)

    unread_list = _live_unread_query(db, user)
    if not unread_list:
        raise ValueError(
            "Nothing to preview yet — this account has no unread nibble. "
            "Open the app once to generate today's bite, then try again."
        )
    bite, item_title, item_id = _feature_bite(db, user, unread_list, today)
    if not bite:
        raise ValueError("Nothing to preview yet — the unread bite's source is no longer available.")

    streak = db.query(Streak).filter(Streak.user_id == user.id).first()
    streak_at_risk = (
        streak and streak.current_streak
        and not (streak.last_active_date and streak.last_active_date >= today)
        and any(b.date < today for b in unread_list)
    )
    kind = "streak" if streak_at_risk else ("fresh" if bite.date >= today else "forgotten")
    title, body = build_notification_copy(kind, item_title, bite, today)
    return (title, body, _notification_data(item_id, bite))


async def _notify_delivery_slot(db_factory, now) -> None:
    """Notify users whose delivery time is `now`, with dynamic, book-specific
    copy (Task 3) — one message PER USER now, not a shared title/body batch,
    since each user's featured book/hook differs. Still one chunked HTTP
    call to Expo at the end via send_push_messages.

    Task 4: gated on `notifications_enabled` (a disabled token is kept, not
    deleted, so it must be actively excluded here) and matched via
    `_effective_utc_slot` — recomputed per token from the user's CURRENT
    timezone every tick, not a cached UTC value fetched once at
    registration time, so this can no longer be a plain indexed SQL filter."""
    from app.models.push_token import PushToken
    from app.models.user import User
    from app.config import get_settings
    from app.services.entitlement_service import reconcile_free_lock_state

    settings = get_settings()
    hour, slot = _slot(now)
    today = now.date()

    messages = []
    with db_factory() as db:
        rows = (
            db.query(PushToken, User)
            .join(User, User.id == PushToken.user_id)
            .filter(PushToken.notifications_enabled.is_(True))
            .all()
        )
        if not rows:
            return
        by_user: dict[str, list[str]] = {}
        user_by_id = {}
        for tok, user in rows:
            eff_hour, eff_minute = _effective_utc_slot(
                tok.notification_local_hour, tok.notification_local_minute,
                user.timezone, tok.notification_hour, tok.notification_minute, now,
            )
            if (eff_hour, eff_minute) != (hour, slot):
                continue
            by_user.setdefault(user.id, []).append(tok.token)
            user_by_id[user.id] = user
        if not by_user:
            return
        for user_id, toks in by_user.items():
            user = user_by_id[user_id]
            # This tick never goes through get_current_user, so lock state
            # must be reconciled here too — a user whose trial/subscription
            # expired since the last _prepare_user_nibbles tick must not
            # keep getting "fresh nibble" pushes for a source that's now
            # locked (see _live_unread_query, which excludes it below).
            reconcile_free_lock_state(db, user)
            # Origin-agnostic: a session the user generated themselves (manual/
            # prefetched) is still today's session — remind about it the same way.
            unread_list = _live_unread_query(db, user)
            if not unread_list:
                continue  # nothing prepared (no active/unlocked sources or gen failed) → no push
            bite, item_title, item_id = _feature_bite(db, user, unread_list, today)
            if not bite:
                continue
            kind = "fresh" if bite.date >= today else "forgotten"
            title, body = build_notification_copy(kind, item_title, bite, today)
            data = _notification_data(item_id, bite)
            for tok in toks:
                messages.append({"to": tok, "title": title, "body": body, "sound": "default", "data": data})

    if messages:
        logger.info("Delivering %d dynamic delivery-slot push(es) (%02d:%02d UTC)", len(messages), hour, slot)
        await send_push_messages(messages, getattr(settings, "expo_access_token", ""))


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
    from app.models.streak import Streak
    from app.models.user import User
    from app.config import get_settings
    from app.services.entitlement_service import reconcile_free_lock_state

    settings = get_settings()
    hour, slot = _slot(now + timedelta(minutes=65))
    today = now.date()

    messages = []
    with db_factory() as db:
        rows = (
            db.query(PushToken, User)
            .join(User, User.id == PushToken.user_id)
            .filter(
                PushToken.notifications_enabled.is_(True),
                PushToken.streak_alerts_enabled.is_(True),
            )
            .all()
        )
        if not rows:
            return
        by_user: dict[str, list[str]] = {}
        user_by_id = {}
        for tok, user in rows:
            eff_hour, eff_minute = _effective_utc_slot(
                tok.notification_local_hour, tok.notification_local_minute,
                user.timezone, tok.notification_hour, tok.notification_minute, now,
            )
            if (eff_hour, eff_minute) != (hour, slot):
                continue
            by_user.setdefault(user.id, []).append(tok.token)
            user_by_id[user.id] = user
        if not by_user:
            return
        for user_id, toks in by_user.items():
            streak = db.query(Streak).filter(Streak.user_id == user_id).first()
            if not streak or not streak.current_streak:
                continue
            if streak.last_active_date and streak.last_active_date >= today:
                continue  # already read today — nothing at risk
            user = user_by_id[user_id]
            # Same reasoning as _notify_delivery_slot: this tick must not
            # alert about a streak-saving session on a source that's since
            # become locked (that bite is unreachable — see
            # _live_unread_query — so "finish it now" would be a dead end).
            reconcile_free_lock_state(db, user)
            held_list = [b for b in _live_unread_query(db, user) if b.date < today]
            if not held_list:
                continue  # no session whose window is closing
            bite, item_title, item_id = _feature_bite(db, user, held_list, today)
            if not bite:
                continue
            title, body = build_notification_copy("streak", item_title, bite, today)
            data = _notification_data(item_id, bite)
            for tok in toks:
                messages.append({"to": tok, "title": title, "body": body, "sound": "default", "data": data})

    if messages:
        logger.info("Streak alert: %d dynamic push(es) (%02d:%02d UTC delivery)", len(messages), hour, slot)
        await send_push_messages(messages, getattr(settings, "expo_access_token", ""))


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


async def _run_task2_maintenance_cycle(db_factory) -> None:
    """Task 2 consolidated backend pass — the real, registered production
    job that closes the "autonomous retry, never restart-only" requirement
    for every durable queue this codebase maintains:
      1. `cleanup_tasks` (Pinecone/S3 compensating cleanup after an
         abandoned/rejected attempt) — see entitlement_service.
         retry_cleanup_tasks.
      2. `reconciliation_tasks` (the mixed-version-cutover fencing queue)
         — see entitlement_service.retry_reconciliation_tasks.
      3. Tombstoned `library_items` whose deletion cleanup previously
         failed (Task 2 closeout, Verified Blocker 6) — see
         entitlement_service.retry_item_deletions.
      4. Pending/failed account erasures (Task 2 closeout, Verified
         Blocker 8) — see entitlement_service.retry_account_erasures.
    Runs every 5 minutes, same cadence as the notification cycle. Each
    queue is processed independently — one raising does not block the
    others — and each queue's own runner already processes its rows
    independently under the SAME claim-lease discipline, so one row's
    failure can never block an unrelated row in the same tick. Blocking
    (real DB work), so it runs in a threadpool to spare the event loop,
    matching `_prepare_user_nibbles`'s own pattern above.
    """
    import asyncio
    from app.services import entitlement_service as ent

    def _run_cleanup():
        with db_factory() as db:
            resolved, failed = ent.retry_cleanup_tasks(db)
            if resolved or failed:
                logger.info("[task2-maintenance] cleanup retry: resolved=%d failed=%d", resolved, failed)

    def _run_reconciliation():
        with db_factory() as db:
            resolved, failed = ent.retry_reconciliation_tasks(db)
            if resolved or failed:
                logger.info("[task2-maintenance] reconciliation retry: resolved=%d failed=%d", resolved, failed)

    def _run_item_deletions():
        with db_factory() as db:
            resolved, failed = ent.retry_item_deletions(db)
            if resolved or failed:
                logger.info("[task2-maintenance] item-deletion retry: resolved=%d failed=%d", resolved, failed)

    def _run_account_erasures():
        with db_factory() as db:
            resolved, failed = ent.retry_account_erasures(db)
            if resolved or failed:
                logger.info("[task2-maintenance] account-erasure retry: resolved=%d failed=%d", resolved, failed)

    try:
        await asyncio.to_thread(_run_cleanup)
    except Exception as e:
        logger.error("[task2-maintenance] cleanup-task retry pass failed: %s", e)

    try:
        await asyncio.to_thread(_run_reconciliation)
    except Exception as e:
        logger.error("[task2-maintenance] reconciliation-task retry pass failed: %s", e)

    try:
        await asyncio.to_thread(_run_item_deletions)
    except Exception as e:
        logger.error("[task2-maintenance] item-deletion retry pass failed: %s", e)

    try:
        await asyncio.to_thread(_run_account_erasures)
    except Exception as e:
        logger.error("[task2-maintenance] account-erasure retry pass failed: %s", e)


def start_scheduler(db_factory) -> None:
    """
    Start the APScheduler with the daily notification job and the Task 2
    cleanup/reconciliation maintenance job.
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
    scheduler.add_job(
        _run_task2_maintenance_cycle,
        trigger="cron",
        minute="*/5",
        kwargs={"db_factory": db_factory},
        id="task2_cleanup_reconciliation",
        replace_existing=True,
        misfire_grace_time=240,
        max_instances=1,
    )
    scheduler.start()
    logger.info("Notification scheduler started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
