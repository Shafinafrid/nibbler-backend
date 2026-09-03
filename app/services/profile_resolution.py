"""Growth-profile resolution, assignment repair and per-user serialization.

THE single source of truth for "which growth profile does this book feed?".
Both `/connect/insights` (goal-match scoring) and session generation import
from here, because when those two disagree the product lies to the user: the
match percentage is measured against one profile while the nibbles are
written for another. That divergence was real — Connect used to send whatever
profile happened to be `activeProfileId`, ignoring the book's own assignment
entirely.

## The three assignment states

A wisdom book is in exactly one of these, and conflating them loses data:

| state                  | growth_profile_id | growth_profile_name | repaired by            |
|------------------------|-------------------|---------------------|------------------------|
| assigned               | set               | server-derived      | —                      |
| bootstrap-unassigned   | NULL              | **NULL**            | attach_unassigned_...  |
| unresolved legacy      | NULL              | **set** (no match)  | promote_resolvable_... |

`growth_profile_name` is the discriminator. A bootstrap row was created
before the user's profile row existed server-side (local-first onboarding
never creates one — see `_get_or_create_profile`), so it has no name either
and can be safely auto-attached. An unresolved-legacy row DOES carry a name
the user once chose; the backfill simply couldn't match it to exactly one
live profile. Overwriting that name would destroy the only evidence of the
user's original intent, so those rows are left alone until either the name
becomes uniquely matchable (promotion) or an entitled user repairs it by
hand in the app.

Story-mode books hold BOTH fields NULL by rule and are outside this table.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Per-user serialization ───────────────────────────────────────────────────
# Lock ordering, used IDENTICALLY on every path that touches profiles or
# assignments (ensure, growth PUT, profile create/rename/delete, assignment
# writes, repair and promotion):
#
#   1. user-scoped advisory transaction lock   <- this function
#   2. profile row lock (SELECT ... FOR UPDATE)
#   3. affected library rows
#
# Acquiring in any other order risks deadlock between two of our own paths.
#
# Why an advisory lock is needed AT ALL when the profile row is already
# locked: a `SELECT ... FOR UPDATE` on `profiles` does not serialize a
# concurrent INSERT into `library_items` that hasn't committed yet. The race
# it closes:
#
#   T1 ensure : locks profile row, attaches every VISIBLE unassigned book, commits
#   T2 create : (started earlier) commits its unassigned book AFTER T1's snapshot
#             -> that book is never attached by anyone
#
# The advisory lock makes both paths queue on the same user-scoped key, so
# T2's insert is either visible to T1's sweep or happens after it. The
# idempotent repair pass below is the belt to this braces — it also catches
# rows that predate this deploy.
_ADVISORY_LOCK_NAMESPACE = 0x6E62_0001  # "nb" + growth-profile namespace


def lock_user_scope(db: Session, user_id: str) -> None:
    """Take the user-scoped advisory lock for the CURRENT transaction.

    Released automatically at commit/rollback (`pg_advisory_xact_lock`) —
    never leaked by an early return or an exception, which is why the xact
    variant is used rather than the session-scoped one.

    No-ops on SQLite (tests): there is no cross-connection concurrency to
    serialize there, and the statement doesn't exist.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(:ns, :key)"),
        {"ns": _ADVISORY_LOCK_NAMESPACE, "key": _stable_int_key(user_id)},
    )


def _stable_int_key(user_id: str) -> int:
    """A stable 32-bit signed key for a Firebase uid.

    Deliberately NOT Python's `hash()`: that is randomized per process by
    PYTHONHASHSEED, so two workers would compute different keys for the same
    user and the lock would serialize nothing at all.
    """
    import zlib

    return zlib.crc32(user_id.encode("utf-8")) - 0x8000_0000


# ── Name normalization ───────────────────────────────────────────────────────

def normalize_profile_name(name: Optional[str]) -> str:
    """Case- and whitespace-insensitive key used for uniqueness and matching.

    Two profiles whose names differ only by case or padding are treated as
    the same name — otherwise "My Goals" and "my goals " are two profiles the
    user cannot tell apart, and a legacy row naming either is ambiguous.
    """
    return " ".join((name or "").split()).casefold()


def live_profiles(growth_state: dict, tombstones=None) -> list[dict]:
    """Profiles that still exist: present in growth_state and not tombstoned."""
    profiles = (growth_state or {}).get("profiles") or []
    dead = set(tombstones or [])
    return [
        p for p in profiles
        if isinstance(p, dict) and p.get("id") and p.get("id") not in dead
    ]


def profile_display_name(profile: dict) -> Optional[str]:
    """The canonical display name of a profile record.

    `profileName` is what the app writes; `name` is the older field. Both are
    kept in sync by the client's own rename, but the server must tolerate
    either being the populated one.
    """
    if not isinstance(profile, dict):
        return None
    return profile.get("profileName") or profile.get("name")


def find_profile_by_id(growth_state: dict, profile_id: str, tombstones=None):
    if not profile_id:
        return None
    for p in live_profiles(growth_state, tombstones):
        if p.get("id") == profile_id:
            return p
    return None


def find_unique_profile_by_name(growth_state: dict, name: str, tombstones=None):
    """The single live profile matching `name`, or None if 0 or 2+ match.

    Ambiguity returns None ON PURPOSE. Picking "the first match" would
    silently bind a book to an arbitrary one of two identically-named
    profiles, and the user would have no way to see that it happened.
    """
    key = normalize_profile_name(name)
    if not key:
        return None
    matches = [
        p for p in live_profiles(growth_state, tombstones)
        if normalize_profile_name(profile_display_name(p)) == key
    ]
    return matches[0] if len(matches) == 1 else None


# ── The resolver ─────────────────────────────────────────────────────────────

def resolve_assigned_profile(growth_state: dict, item, tombstones=None) -> Optional[dict]:
    """The growth profile `item` actually feeds.

    Precedence, in order:
      1. stable `growth_profile_id`
      2. unique legacy-name match (ambiguous names are skipped, never guessed)
      3. the active profile
      4. the first profile
      5. none (the user genuinely has no profiles yet — pre-first-sync)

    Steps 3-4 are a FALLBACK for display and scoring continuity, not an
    assignment: they do not write anything back to the row. Note that
    `activeProfileId` is only ever set at onboarding and on delete-fallback —
    there is no way for a user to change it — so in practice step 3 resolves
    to the onboarding profile. That is exactly why the per-book assignment
    (step 1) is the only real steering mechanism.
    """
    profiles = live_profiles(growth_state, tombstones)
    if not profiles:
        return None

    pid = getattr(item, "growth_profile_id", None)
    if pid:
        match = find_profile_by_id(growth_state, pid, tombstones)
        if match:
            return match

    name = getattr(item, "growth_profile_name", None)
    if name:
        match = find_unique_profile_by_name(growth_state, name, tombstones)
        if match:
            return match

    active_id = (growth_state or {}).get("activeProfileId")
    if active_id:
        match = find_profile_by_id(growth_state, active_id, tombstones)
        if match:
            return match

    return profiles[0]


def is_unresolved_legacy(growth_state: dict, item, tombstones=None) -> bool:
    """True when the row names a profile the server cannot uniquely match.

    Drives the honest "this book's goal couldn't be matched" caption, and
    the REPAIR affordance for entitled users. Deliberately distinct from
    "unassigned": a bootstrap row (both fields NULL) is not unresolved, it is
    simply waiting to be attached.
    """
    if getattr(item, "growth_profile_id", None):
        return find_profile_by_id(growth_state, item.growth_profile_id, tombstones) is None
    name = getattr(item, "growth_profile_name", None)
    if not name:
        return False
    return find_unique_profile_by_name(growth_state, name, tombstones) is None


def default_profile(growth_state: dict, tombstones=None) -> Optional[dict]:
    """The profile a new/reset assignment lands on: active, else first."""
    profiles = live_profiles(growth_state, tombstones)
    if not profiles:
        return None
    active_id = (growth_state or {}).get("activeProfileId")
    if active_id:
        match = find_profile_by_id(growth_state, active_id, tombstones)
        if match:
            return match
    return profiles[0]


def build_profile_payload(profile: Optional[dict]) -> dict:
    """The growth_profile shape the generation/scoring paths consume.

    Mirrors the app's `sessionPrefetch.buildSessionPayload`. `id` MUST be
    present: without it a personalization answer cannot be attributed to the
    profile that actually fed the question, and falls back to "whatever is
    active now" — the bug fixed in Aug 2026.
    """
    if not profile:
        return {}
    interests = [
        (i.get("tag") if isinstance(i, dict) else i)
        for i in (profile.get("interests") or [])
    ]
    return {
        "id": profile.get("id"),
        "name": profile_display_name(profile),
        "lifeArea": profile.get("lifeArea"),
        "aspirationLabel": profile.get("aspirationLabel"),
        "aspirationUnderstanding": profile.get("aspirationUnderstanding"),
        "confidenceStyle": profile.get("confidenceStyle"),
        "goalOrientation": profile.get("goalOrientation"),
        "contentMode": profile.get("contentMode"),
        "interests": [i for i in interests if i],
    }


def scoring_fingerprint(profile: Optional[dict]) -> Optional[str]:
    """Stable digest of the profile inputs a score was actually computed from.

    Returned to the client alongside `resolved_profile_id` so a cached score
    can be proven to belong to the profile currently assigned — a book/day
    cache key alone silently served the previous profile's percentage after a
    switch on the same day.

    Only the fields that genuinely feed retrieval/scoring are included, so an
    unrelated edit (pacing, streak) doesn't needlessly invalidate a cache.
    Sorted keys + separators make this byte-stable across processes.
    """
    if not profile:
        return None
    import hashlib
    import json

    payload = build_profile_payload(profile)
    material = {
        "id": payload.get("id"),
        "name": payload.get("name"),
        "lifeArea": payload.get("lifeArea"),
        "aspirationLabel": payload.get("aspirationLabel"),
        "aspirationUnderstanding": payload.get("aspirationUnderstanding"),
        "interests": sorted(payload.get("interests") or []),
        "contentMode": payload.get("contentMode"),
        "goalOrientation": payload.get("goalOrientation"),
    }
    blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ── Repair passes ────────────────────────────────────────────────────────────
# Both are idempotent and safe to call on every ensure/growth-write, and on
# library reads and scheduler runs. Caller must already hold the user scope
# lock (see lock_user_scope) and is responsible for the commit.

def attach_unassigned_wisdom_books(db: Session, user_id: str, growth_state: dict,
                                   tombstones=None) -> int:
    """Attach BOOTSTRAP-unassigned wisdom books to the default profile.

    Target — both assignment fields NULL:

        growth_profile_id IS NULL AND growth_profile_name IS NULL AND mode='wisdom'

    The `growth_profile_name IS NULL` half is load-bearing: it is the only
    thing separating a bootstrap row (created before the profile row existed,
    safe to auto-attach) from an unresolved-legacy row (carries a name the
    user chose, which must never be overwritten). Dropping that condition
    would silently destroy legacy assignment intent.

    Returns the number of rows attached.
    """
    from app.models.library import LibraryItem

    target = default_profile(growth_state, tombstones)
    if not target:
        return 0

    rows = (
        db.query(LibraryItem)
        .filter(
            LibraryItem.user_id == user_id,
            LibraryItem.growth_profile_id.is_(None),
            LibraryItem.growth_profile_name.is_(None),
            LibraryItem.mode == "wisdom",
        )
        .all()
    )
    if not rows:
        return 0

    name = profile_display_name(target)
    for row in rows:
        row.growth_profile_id = target.get("id")
        row.growth_profile_name = name
    logger.info(
        "attached %d bootstrap-unassigned wisdom book(s) for %s to profile %s",
        len(rows), user_id, target.get("id"),
    )
    return len(rows)


def promote_resolvable_legacy_rows(db: Session, user_id: str, growth_state: dict,
                                   tombstones=None) -> int:
    """Give a stable id to legacy rows whose name is NOW uniquely matchable.

    Target — no id but a name:

        growth_profile_id IS NULL AND growth_profile_name IS NOT NULL

    A row is promoted only when its normalized name matches EXACTLY ONE live
    profile. Zero-match and ambiguous rows are left untouched: they keep
    their name, stay "unresolved legacy", and remain repairable by an
    entitled user in the app. This covers users whose library rows reached
    the server before their growth state did, and rows that become
    matchable later (e.g. the duplicate that made them ambiguous is deleted).

    Returns the number of rows promoted.
    """
    from app.models.library import LibraryItem

    if not live_profiles(growth_state, tombstones):
        return 0

    rows = (
        db.query(LibraryItem)
        .filter(
            LibraryItem.user_id == user_id,
            LibraryItem.growth_profile_id.is_(None),
            LibraryItem.growth_profile_name.isnot(None),
        )
        .all()
    )
    promoted = 0
    for row in rows:
        match = find_unique_profile_by_name(growth_state, row.growth_profile_name, tombstones)
        if not match:
            continue  # zero-match or ambiguous — leave it alone, on purpose
        row.growth_profile_id = match.get("id")
        row.growth_profile_name = profile_display_name(match)
        promoted += 1
    if promoted:
        logger.info("promoted %d legacy assignment row(s) for %s", promoted, user_id)
    return promoted


def redetermine_assignment_names(db: Session, user_id: str, growth_state: dict,
                                 tombstones=None) -> int:
    """Re-derive every assigned row's display name from its stable id.

    This is what makes rename automatically safe: books carry the id, so a
    rename only has to refresh the denormalized snapshot, in the SAME
    transaction as the rename itself. Nothing is ever orphaned, and no
    per-item client PATCH is needed — the backend owns both datasets.

    Returns the number of rows whose stored name actually changed.
    """
    from app.models.library import LibraryItem

    rows = (
        db.query(LibraryItem)
        .filter(
            LibraryItem.user_id == user_id,
            LibraryItem.growth_profile_id.isnot(None),
        )
        .all()
    )
    changed = 0
    for row in rows:
        match = find_profile_by_id(growth_state, row.growth_profile_id, tombstones)
        if not match:
            continue
        name = profile_display_name(match)
        if row.growth_profile_name != name:
            row.growth_profile_name = name
            changed += 1
    return changed


def reassign_books_from_deleted_profile(db: Session, user_id: str, deleted_profile_id: str,
                                        growth_state: dict, tombstones=None) -> int:
    """Move books off a deleted profile, deterministically.

    Target, in order:
      1. `activeProfileId`, EXCLUDING the profile being deleted
      2. else the first surviving profile
      3. else NULL — defensive only. The client refuses to delete the last
         profile, so this should be unreachable; it is handled rather than
         left undefined so a corrupt state can't produce a dangling id.

    Runs in the same transaction as the tombstone write and the
    `activeProfileId` repair.
    """
    from app.models.library import LibraryItem

    survivors = [
        p for p in live_profiles(growth_state, tombstones)
        if p.get("id") != deleted_profile_id
    ]
    target = None
    if survivors:
        active_id = (growth_state or {}).get("activeProfileId")
        if active_id and active_id != deleted_profile_id:
            target = next((p for p in survivors if p.get("id") == active_id), None)
        target = target or survivors[0]

    rows = (
        db.query(LibraryItem)
        .filter(
            LibraryItem.user_id == user_id,
            LibraryItem.growth_profile_id == deleted_profile_id,
        )
        .all()
    )
    for row in rows:
        row.growth_profile_id = target.get("id") if target else None
        row.growth_profile_name = profile_display_name(target) if target else None
    if rows:
        logger.info(
            "reassigned %d book(s) from deleted profile %s to %s",
            len(rows), deleted_profile_id, (target or {}).get("id"),
        )
    return len(rows)
