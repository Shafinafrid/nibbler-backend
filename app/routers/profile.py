from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session
from app.database import get_db
from app.middleware.auth import get_current_user
from app.models.user import User
from app.models.profile import Profile
from app.rate_limit import limiter
from app.schemas.profile import (
    ProfileCreate,
    ProfileUpdate,
    ProfileResponse,
    AspirationRequest,
    AspirationResult,
    GrowthStateUpdate,
)
from app.services.llm import LLMService
import logging
import uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["profile"])


def _get_or_create_profile(user: User, db: Session, *, for_update: bool = False) -> Profile:
    """Local-first onboarding never creates a backend profile row, so the
    row is created lazily — a 404 here used to break every local-onboarded
    user (and legacy /bites/today).

    Re-audit finding #7-3 (Sep 2026): `for_update=True` takes a real
    `SELECT ... FOR UPDATE` row lock, matching this codebase's established
    idiom (entitlement_service.py, delivery_lifecycle.py, connect.py all use
    the same primitive for a serialized read-modify-write). `user.profile`
    is a relationship attribute that may already be populated from an
    earlier, UNLOCKED read on this same request (e.g. get_current_user's own
    query) — trusting it here would make `for_update` a no-op in exactly the
    case that matters, so a locked caller always re-queries explicitly with
    `populate_existing()` to force a fresh, lock-holding read from the
    database rather than SQLAlchemy's identity map handing back the
    already-loaded (unlocked) object.
    """
    if for_update and user.profile:
        return (
            db.query(Profile)
            .filter(Profile.id == user.profile.id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    if user.profile:
        return user.profile
    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=user.id,
        name=user.display_name or (user.email or "").split("@")[0] or "Nibbler user",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    db.refresh(user)
    if for_update:
        return (
            db.query(Profile)
            .filter(Profile.id == profile.id)
            .populate_existing()
            .with_for_update()
            .first()
        )
    return profile


def _filter_tombstoned_profiles(profile: Profile) -> bool:
    """Defense in depth (finding #7, Sep 2026): strip any tombstoned profile
    out of profile.growth_state['profiles'] in place, mutating the ORM object
    but WITHOUT committing here — callers decide whether/when to persist.
    Returns True iff anything was actually filtered out.

    This exists as a second enforcement point beyond the PUT-time filter (see
    update_growth_state) in case anything ever writes growth_state without
    going through that path. It must never resurrect data by itself — it only
    removes, never adds, and it's a no-op when there's nothing to filter.
    """
    gs = profile.growth_state
    tombstones = set((profile.deleted_profile_ids or []))
    if not gs or not tombstones:
        return False
    profiles = gs.get("profiles")
    if not isinstance(profiles, list):
        return False
    filtered = [p for p in profiles if not (isinstance(p, dict) and p.get("id") in tombstones)]
    if len(filtered) == len(profiles):
        return False
    # A brand-new dict, not an in-place mutation of the existing one: the
    # JSON column type doesn't track in-place mutation of the object it
    # already holds, so reassigning the SAME dict reference (even with a
    # different `profiles` key inside it) is invisible to SQLAlchemy's dirty
    # tracking and silently fails to persist — proven by direct reproduction
    # during this fix's own testing.
    profile.growth_state = {**gs, "profiles": filtered}
    return True


@router.get("/", response_model=ProfileResponse)
def get_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    profile = _get_or_create_profile(current_user, db)
    # Defense in depth: filter tombstoned profiles out of the READ response
    # too, in case anything ever wrote growth_state around the PUT path's
    # primary enforcement. Persist the cleanup so it doesn't need to be
    # recomputed on every future read.
    if _filter_tombstoned_profiles(profile):
        db.commit()
        db.refresh(profile)
    return profile


@router.put("/growth", response_model=ProfileResponse)
def update_growth_state(
    data: GrowthStateUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Store the app's full local growth state ({person, profiles[],
    activeProfileId}) so onboarding survives reinstalls and new devices.
    The app pushes on every ProfileRepository.saveState and pulls when its
    local copy is empty at sign-in.

    Re-audit finding #7-3 (Sep 2026): `for_update=True` takes a real row
    lock (`SELECT ... FOR UPDATE`) for the DURATION of this whole
    read-modify-write, held until `db.commit()` below. Before this fix, two
    concurrent requests for the same profile (e.g. device A deleting profile
    X and device B deleting profile Y, syncing at the same moment) each read
    the row's `deleted_profile_ids`/`growth_state` independently, computed
    their own union in Python, and whichever commit landed LAST won outright
    — silently discarding the other request's tombstone/growth_state union
    entirely (reproduced: A's tombstone for X was lost when B's commit
    landed after A's). The lock serializes the two requests instead: B's
    `SELECT ... FOR UPDATE` blocks until A's transaction commits, then B
    reads A's ALREADY-COMMITTED union as its own starting point — so the
    second writer's union is computed from up-to-date state, not a stale
    snapshot, and no tombstone or edit from either side is lost.
    """
    profile = _get_or_create_profile(current_user, db, for_update=True)

    # Deletion tombstones (finding #7, Sep 2026) — applied FIRST, before either
    # existing guard below, and unconditionally (regardless of which side
    # "wins" the LWW timestamp compare further down). This is the whole point:
    # a stale device that never learned about a deletion can still push a
    # LATER timestamp than the device that deleted it, because the deletion
    # and the stale device's unrelated edit are two genuinely independent
    # events — wall-clock order between them proves nothing about which one
    # reflects the deletion. So the tombstone set, not the timestamp, is the
    # source of truth for "is this profile gone," and it is enforced on the
    # incoming data itself before any winner is decided.
    #
    # UNION only — an id is never removed from this set here. "Undelete" is
    # explicitly out of scope for this mechanism.
    incoming_tombstones = set(data.deletedProfileIds or [])
    stored_tombstones = set(profile.deleted_profile_ids or [])
    union_tombstones = stored_tombstones | incoming_tombstones

    incoming_growth_state = dict(data.growth_state or {})
    raw_new_profiles = incoming_growth_state.get("profiles")
    if isinstance(raw_new_profiles, list) and union_tombstones:
        incoming_growth_state["profiles"] = [
            p for p in raw_new_profiles
            if not (isinstance(p, dict) and p.get("id") in union_tombstones)
        ]

    # Defense in depth: a client-side bug (2026-07-25 — a fresh install could
    # end up pushing throwaway pre-signup onboarding data over a real profile)
    # was able to permanently erase this column with nothing but a normal PUT.
    # The client fix removes the way that happened, but the row itself should
    # never trust a client enough to let one push wipe real data down to zero
    # profiles — that's not a legitimate edit under any normal product flow.
    #
    # Both sides are tombstone-filtered before this comparison: existing_profiles
    # is filtered so a profile everyone already agrees is deleted doesn't count
    # as "existing data" that would block a legitimate empty push, and
    # new_profiles is the already-filtered incoming list from above — so if a
    # push's ENTIRE profiles[] turns out to be tombstoned ids, that correctly
    # looks like (and is treated as) an empty push, and this guard still fires
    # if the server has other real, non-tombstoned profiles on file.
    stored_raw_profiles = ((profile.growth_state or {}).get("profiles")) or []
    existing_profiles = [
        p for p in stored_raw_profiles
        if not (isinstance(p, dict) and p.get("id") in union_tombstones)
    ]
    new_profiles = incoming_growth_state.get("profiles") or []
    if existing_profiles and not new_profiles:
        raise HTTPException(
            status_code=409,
            detail="Refusing to overwrite an existing growth profile with an empty one.",
        )

    # Persist the unioned tombstone set regardless of how the LWW compare
    # below resolves — even a push whose growth_state is ignored as stale
    # still contributed real deletion knowledge that must never be lost.
    if union_tombstones != stored_tombstones:
        profile.deleted_profile_ids = sorted(union_tombstones)

    # Last-writer-wins by timestamp, so a device that has been offline can't
    # push a stale profile over newer edits made elsewhere. Before this there
    # was no version field at all: outside the special "unreconciled" path in
    # AppContext.init(), whichever device saved last simply won, however old
    # its copy was.
    #
    # Returned as a normal 200 rather than a 409 — the client's push is
    # correctly a no-op, not an error, and a 4xx here would make the outbox
    # treat it as permanently rejected. Only applied when BOTH sides carry a
    # timestamp, so older clients keep working unchanged.
    incoming_at = incoming_growth_state.get("updatedAt")
    stored_at = (profile.growth_state or {}).get("updatedAt")
    if incoming_at and stored_at and str(incoming_at) < str(stored_at):
        logger.info(
            "growth push ignored for %s: incoming %s is older than stored %s",
            current_user.id, incoming_at, stored_at,
        )
        # The growth_state BODY is dropped as stale (existing LWW behavior,
        # unchanged), but the tombstone union above must still take effect —
        # including against the STORED profiles[] this response is about to
        # return. Without this, a push whose only new information is a
        # deletion (and which loses LWW on its unrelated growth_state
        # timestamp) would have its tombstone persisted but the already-
        # stored profiles[] would still show the now-deleted profile until
        # some later push's growth_state happens to win and re-triggers the
        # filter above.
        _filter_tombstoned_profiles(profile)
        db.commit()
        db.refresh(profile)
        return profile

    profile.growth_state = incoming_growth_state
    person_name = ((incoming_growth_state.get("person") or {}).get("name") or "").strip()
    if person_name:
        profile.name = person_name
    db.commit()
    db.refresh(profile)
    return profile


@router.put("/", response_model=ProfileResponse)
def update_profile(
    data: ProfileUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not current_user.profile:
        raise HTTPException(status_code=404, detail="Profile not found.")

    for field, value in data.model_dump(exclude_none=True).items():
        setattr(current_user.profile, field, value)

    db.commit()
    db.refresh(current_user.profile)
    return current_user.profile


@router.post("/complete-onboarding", response_model=ProfileResponse)
def complete_onboarding(
    data: ProfileCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save the completed onboarding profile."""
    if current_user.profile:
        # Update existing profile
        for field, value in data.model_dump(exclude_none=True).items():
            setattr(current_user.profile, field, value)
        db.commit()
        db.refresh(current_user.profile)
        return current_user.profile

    profile = Profile(
        id=str(uuid.uuid4()),
        user_id=current_user.id,
        **data.model_dump(exclude_none=True),
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/interpret-aspiration", response_model=AspirationResult)
@limiter.limit("10/hour")  # per-IP (unauthenticated); ~3 calls per real onboarding
def interpret_aspiration(request: Request, data: AspirationRequest):
    """
    Onboarding aspiration interpreter (moved server-side July 2026 so the
    Anthropic key never ships in the app binary).

    Deliberately unauthenticated: onboarding runs before account creation.
    Kept cheap and abuse-resistant via the 500-char input cap, a small
    max_tokens budget, and per-IP rate limiting.
    """
    llm = LLMService()
    return llm.interpret_aspiration(data.answer)


# NOTE (July 2026): POST /profile/onboarding/chat was retired here. It served
# the old conversational-interview onboarding, which the app replaced with the
# local-first aspiration flow (POST /profile/interpret-aspiration above).
