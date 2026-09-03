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
    GrowthPushResult,
    GrowthProfileCreate,
    GrowthProfileRename,
)
from app.services.llm import LLMService
from app.services.growth_merge import merge_growth_state, CANONICAL_NAME_FLAG
from app.services.profile_resolution import (
    attach_unassigned_wisdom_books,
    find_profile_by_id,
    live_profiles,
    lock_user_scope,
    normalize_profile_name,
    profile_display_name,
    promote_resolvable_legacy_rows,
    reassign_books_from_deleted_profile,
    redetermine_assignment_names,
)
import logging
import uuid

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/profile", tags=["profile"])


def _sync_assignments(db: Session, user_id: str, growth_state: dict, tombstones) -> None:
    """Keep library assignments consistent with the profile set.

    Runs inside the caller's transaction and lock, on every path that can
    change which profiles exist or what they are called:

      1. attach bootstrap rows (both assignment fields NULL) — these were
         created before the profile row existed server-side and have no
         name to preserve
      2. promote legacy rows whose name is NOW uniquely matchable
      3. re-derive the display-name snapshot on every assigned row, which is
         what makes rename automatically safe

    Never raises into the request: a repair pass failing must not take down
    an otherwise-valid growth push. The passes are idempotent, so whatever
    fails here is simply retried on the next call.
    """
    try:
        attach_unassigned_wisdom_books(db, user_id, growth_state, tombstones)
        promote_resolvable_legacy_rows(db, user_id, growth_state, tombstones)
        redetermine_assignment_names(db, user_id, growth_state, tombstones)
    except Exception:
        logger.exception("assignment sync failed for %s (non-fatal)", user_id)


def _growth_push_result(profile: Profile, merged: dict, user: User) -> GrowthPushResult:
    """Attach the reconciliation payload to the normal profile response."""
    result = GrowthPushResult.model_validate(profile, from_attributes=True)
    result.rejectedProfileIds = merged.get("rejected_profile_ids") or []
    result.acceptedProfileIds = merged.get("accepted_profile_ids") or []
    result.canonicalProfileFields = merged.get("canonical_profile_fields") or []
    result.effectivePremium = bool(user.effective_premium)
    return result


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


@router.put("/growth", response_model=GrowthPushResult)
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
    # Lock ordering, step 1 of 3 (advisory -> profile row -> library rows).
    # The profile row lock alone does not serialize a concurrent library
    # INSERT that hasn't committed, so ensure/create could race and strand a
    # freshly-created book with no assignment. Every path that touches
    # profiles or assignments takes these in this same order.
    lock_user_scope(db, current_user.id)
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

    # ── ID-aware merge (Sep 2026) ────────────────────────────────────────
    #
    # This used to assign the incoming blob WHOLESALE, with a single
    # root-level last-writer-wins compare deciding the fate of the entire
    # profiles array. That is unsafe now that profiles can also be created
    # and renamed through their own canonical endpoints:
    #
    #   · a stale push carrying only {A} would DELETE a profile B that was
    #     just created canonically — with no tombstone anywhere
    #   · a stale push carrying an old name would REVERT a canonical rename,
    #     because an unrelated personalization answer bumps that profile's
    #     timestamp and makes the stale body look newer
    #
    # merge_growth_state applies: absence != deletion, tombstones always win
    # and block recreation, per-profile whole-body LWW on each profile's OWN
    # parsed timestamp (ties keep the stored body), canonical fields restored
    # afterwards, and explicit per-key rules for the root fields.
    #
    # Note the old compare was `str(incoming) < str(stored)` — a raw string
    # compare that orders differing UTC offsets and fractional precisions
    # wrongly. Timestamps are parsed to instants now.
    allow_new = bool(current_user.effective_premium)
    merged = merge_growth_state(
        (profile.growth_state or {}),
        incoming_growth_state,
        tombstones=union_tombstones,
        allow_new_profile_ids=allow_new,
    )

    if merged["rejected_profile_ids"]:
        logger.info(
            "growth push for %s: rejected %d unentitled new profile id(s): %s",
            current_user.id, len(merged["rejected_profile_ids"]),
            merged["rejected_profile_ids"],
        )
    if merged["tie_conflicts"]:
        logger.info(
            "growth push for %s: %d profile(s) had equal timestamps with "
            "differing bodies; kept the stored body: %s",
            current_user.id, len(merged["tie_conflicts"]), merged["tie_conflicts"],
        )

    profile.growth_state = merged["state"]
    person_name = ((merged["state"].get("person") or {}).get("name") or "").strip()
    if person_name:
        profile.name = person_name

    # Keep assignments consistent with whatever the merge settled on, inside
    # this same transaction and lock: attach bootstrap rows created before
    # the profile row existed, promote legacy rows whose name is now uniquely
    # matchable, and refresh every assigned row's derived display name.
    _sync_assignments(db, current_user.id, merged["state"], union_tombstones)

    db.commit()
    db.refresh(profile)
    return _growth_push_result(profile, merged, current_user)


def _require_premium_for_profiles(user: User):
    if not user.effective_premium:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "premium_required",
                "message": "Running several growth journeys at once is available with Nibbler Pro.",
            },
        )


def _reject_duplicate_name(growth_state: dict, name: str, tombstones, *, exclude_id=None):
    """Refuse a name that collides with another LIVE profile of this user.

    Uniqueness is what makes legacy name-matching decidable at all: with two
    profiles called "Money", a book that names one is ambiguous forever and
    can never be promoted to a stable id.
    """
    key = normalize_profile_name(name)
    for p in live_profiles(growth_state, tombstones):
        if exclude_id and p.get("id") == exclude_id:
            continue
        if normalize_profile_name(profile_display_name(p)) == key:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "duplicate_profile_name",
                    "message": "You already have a growth profile with that name.",
                },
            )


@router.post("/growth/ensure", response_model=ProfileResponse)
def ensure_growth_state(
    data: GrowthStateUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Idempotently make sure this user HAS a backend profile row.

    Local-first onboarding never creates one, so between "onboarded on the
    device" and "first growth push landed" the server genuinely has zero
    profiles. Anything created in that window (an upload, say) cannot be
    assigned to anything yet.

    Calling this before the first library create closes that window. It is
    safe to call repeatedly: an existing row is returned untouched, and only
    the assignment repair passes run.
    """
    lock_user_scope(db, current_user.id)
    profile = _get_or_create_profile(current_user, db, for_update=True)
    tombstones = set(profile.deleted_profile_ids or [])

    existing = live_profiles(profile.growth_state or {}, tombstones)
    if not existing:
        incoming = dict(data.growth_state or {})
        incoming_profiles = [
            p for p in (incoming.get("profiles") or [])
            if isinstance(p, dict) and p.get("id") and p.get("id") not in tombstones
        ]
        if incoming_profiles:
            # Seed from the client's onboarding profile. Only the FIRST is
            # taken: bootstrapping is not a way to create several profiles
            # without entitlement.
            incoming["profiles"] = incoming_profiles[:1]
            incoming["activeProfileId"] = incoming_profiles[0].get("id")
            profile.growth_state = incoming
            person_name = ((incoming.get("person") or {}).get("name") or "").strip()
            if person_name:
                profile.name = person_name

    _sync_assignments(db, current_user.id, profile.growth_state or {}, tombstones)
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/profiles", response_model=GrowthPushResult)
def create_growth_profile(
    data: GrowthProfileCreate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create ONE additional growth profile. Premium, server-enforced.

    This is the authoritative path for current clients. The generic growth
    PUT keeps its own id-diff filter, but only as defence-in-depth for
    pre-update binaries — UI gating is not enforcement, and that endpoint
    accepts a whole array.

    Returns the canonical state so the client can apply it as a TARGETED
    local mutation (insert just this profile by id) rather than overwriting
    its own blob, which would discard personalization events recorded on
    that device while this request was in flight.
    """
    _require_premium_for_profiles(current_user)

    incoming = data.profile or {}
    profile_id = (incoming.get("id") or "").strip()
    name = (profile_display_name(incoming) or "").strip()
    if not profile_id:
        raise HTTPException(status_code=400, detail="A profile id is required.")
    if not name:
        raise HTTPException(status_code=400, detail="A profile name is required.")

    lock_user_scope(db, current_user.id)
    profile = _get_or_create_profile(current_user, db, for_update=True)
    tombstones = set(profile.deleted_profile_ids or [])
    state = dict(profile.growth_state or {})

    if profile_id in tombstones:
        raise HTTPException(
            status_code=409,
            detail="That profile was deleted and cannot be recreated.",
        )
    if find_profile_by_id(state, profile_id, tombstones):
        # Idempotent: a retried create is not an error.
        merged = {"rejected_profile_ids": [], "accepted_profile_ids": [profile_id],
                  "canonical_profile_fields": [], "tie_conflicts": []}
        return _growth_push_result(profile, merged, current_user)

    _reject_duplicate_name(state, name, tombstones)

    body = dict(incoming)
    body["id"] = profile_id
    body["name"] = name
    body["profileName"] = name
    state["profiles"] = list(state.get("profiles") or []) + [body]
    state.setdefault("activeProfileId", profile_id)
    profile.growth_state = state

    _sync_assignments(db, current_user.id, state, tombstones)
    db.commit()
    db.refresh(profile)

    merged = {"rejected_profile_ids": [], "accepted_profile_ids": [profile_id],
              "canonical_profile_fields": [], "tie_conflicts": []}
    return _growth_push_result(profile, merged, current_user)


@router.patch("/profiles/{profile_id}", response_model=GrowthPushResult)
def rename_growth_profile(
    profile_id: str,
    data: GrowthProfileRename,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rename ONE growth profile. Available to EVERY tier.

    Authoritative because books reference a profile by stable id and store a
    DERIVED name snapshot: the rename and the snapshot refresh have to happen
    in the same transaction, or every assigned book is left advertising a
    goal that no longer exists. That was the old orphaning bug — and it
    needed no client PATCH fan-out to fix, because the backend owns both
    datasets.
    """
    new_name = data.name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="A profile name cannot be blank.")

    lock_user_scope(db, current_user.id)
    profile = _get_or_create_profile(current_user, db, for_update=True)
    tombstones = set(profile.deleted_profile_ids or [])
    state = dict(profile.growth_state or {})

    target = find_profile_by_id(state, profile_id, tombstones)
    if not target:
        raise HTTPException(status_code=404, detail="Growth profile not found.")

    _reject_duplicate_name(state, new_name, tombstones, exclude_id=profile_id)

    updated = []
    for p in state.get("profiles") or []:
        if isinstance(p, dict) and p.get("id") == profile_id:
            p = dict(p)
            p["name"] = new_name
            p["profileName"] = new_name
            # Mark the name as CANONICALLY established, so a stale device
            # still holding the old one cannot silently revert it via the
            # generic growth PUT (see growth_merge's I2). Only names set
            # here are defended; an ordinary offline rename through the blob
            # keeps working as it always did.
            p[CANONICAL_NAME_FLAG] = True
        updated.append(p)
    state["profiles"] = updated
    profile.growth_state = state

    # Same transaction: every assigned book's derived snapshot is refreshed,
    # so nothing is orphaned by the rename.
    _sync_assignments(db, current_user.id, state, tombstones)
    db.commit()
    db.refresh(profile)

    merged = {"rejected_profile_ids": [], "accepted_profile_ids": [profile_id],
              "canonical_profile_fields": [], "tie_conflicts": []}
    return _growth_push_result(profile, merged, current_user)


@router.delete("/profiles/{profile_id}", response_model=GrowthPushResult)
def delete_growth_profile(
    profile_id: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete ONE growth profile. Available to every tier.

    In ONE transaction: tombstone the id, repair activeProfileId, and
    deterministically move its books to
    activeProfileId-excluding-the-deleted -> first surviving -> NULL
    (defensive only; the client refuses to delete the last profile).
    """
    lock_user_scope(db, current_user.id)
    profile = _get_or_create_profile(current_user, db, for_update=True)
    tombstones = set(profile.deleted_profile_ids or [])
    state = dict(profile.growth_state or {})

    target = find_profile_by_id(state, profile_id, tombstones)
    if not target:
        raise HTTPException(status_code=404, detail="Growth profile not found.")
    if len(live_profiles(state, tombstones)) <= 1:
        raise HTTPException(
            status_code=409,
            detail="You need at least one growth profile — Nibbler uses it to pick your nibbles.",
        )

    # Books move BEFORE the profile leaves the state, so the reassignment
    # target is computed against the pre-deletion picture.
    reassign_books_from_deleted_profile(db, current_user.id, profile_id, state, tombstones)

    state["profiles"] = [
        p for p in (state.get("profiles") or [])
        if not (isinstance(p, dict) and p.get("id") == profile_id)
    ]
    survivors = [p.get("id") for p in live_profiles(state, tombstones)]
    if state.get("activeProfileId") == profile_id:
        state["activeProfileId"] = survivors[0] if survivors else None
    profile.growth_state = state
    profile.deleted_profile_ids = sorted(tombstones | {profile_id})

    _sync_assignments(db, current_user.id, state, set(profile.deleted_profile_ids))
    db.commit()
    db.refresh(profile)

    merged = {"rejected_profile_ids": [], "accepted_profile_ids": [],
              "canonical_profile_fields": [], "tie_conflicts": []}
    return _growth_push_result(profile, merged, current_user)


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
