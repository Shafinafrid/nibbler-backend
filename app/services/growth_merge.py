"""ID-aware merge of a pushed growth_state into the stored one.

## Why a merge, and not a replacement

`PUT /profile/growth` used to assign the incoming blob wholesale. Once
profiles can also be created and renamed through their own canonical
endpoints, whole-blob replacement actively destroys data:

    Create race
      A: POST /profile/profiles creates profile B      -> server {A, B}
      B: a queued push (stale) carries {A} only, newer timestamp
      replacement -> {A}      ** B destroyed; no tombstone ever existed **

    Rename race
      A: PATCH renames profile P "Old" -> "New"        -> server "New"
      B: answers a question on its stale copy; P.updatedAt bumps; sends "Old"
      replacement -> "Old"    ** canonical rename silently reverted **

So two invariants hold here:

  I1  A profile MISSING from an incoming push means "this device didn't know
      about it", never "delete it". Deletion happens only via an explicit
      tombstone.
  I2  Canonical names change ONLY through the rename endpoint. A stale
      device carrying an old name is indistinguishable from a deliberate
      rename-back — especially when an unrelated answer bumps that profile's
      updatedAt — so the server never infers rename intent from a blob.

## Scope: collection-level, not event-level

The merge chooses, per profile id, ONE COMPLETE BODY. It deliberately does
NOT union individual ledger entries across devices, because `foldLedger`
(the client's reducer) is order-sensitive well beyond clamping:
`engagement.avgSessionLen` is an exponentially weighted running average,
`bestTimeOfDay`/`lastActive` are last-write-wins, `pacing.sessionLength` is
threshold-driven off that average, `currentStreak` is sequential, and
`contentMode` is later-event-wins. Re-sorting entries would change existing
users' derived profiles, and a faithful cross-language fold port is a
distributed-event-log project in its own right.

KNOWN LIMITATION, stated honestly: two devices concurrently editing the SAME
profile's body remain subject to per-profile last-writer-wins, so a losing
body's ledger event can still be lost. That is pre-existing synchronisation
debt, unchanged by this module — not something it claims to solve.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# The fractional-seconds group of an ISO-8601 timestamp, e.g. ".123" in
# 2026-09-01T10:00:00.123Z — captured so it can be normalised to the 6
# digits Python 3.9's fromisoformat insists on.
_FRACTION_RE = re.compile(r"\.(\d+)")

# Fields the server owns. A client may echo them back, but never change them
# through this endpoint — see I2.
CANONICAL_PROFILE_FIELDS = ("id", "name", "profileName")

# Marks a profile whose name was set through the authoritative rename
# endpoint. Only those are defended against a blob overwriting them (I2):
# with no canonical rename on record there is nothing for a stale device to
# revert TO, and freezing every name would break the ordinary offline rename
# that predates that endpoint.
CANONICAL_NAME_FLAG = "_canonicalName"

# Root keys with their own explicit merge rule below. Everything NOT in this
# set is an unknown/future key and follows the generic rule (stored wins once
# stored; adopted from the push the first time it is seen).
_EXPLICIT_ROOT_KEYS = frozenset({
    "profiles", "activeProfileId", "updatedAt", "person", "deletedProfileIds",
})


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp into an aware datetime, or None.

    Never compare these as strings. The previous implementation did
    `str(incoming) < str(stored)`, which is only correct when every client
    emits byte-identical formatting — a different UTC offset, fractional
    precision, or a `Z` suffix sorts wrong, and that comparison alone decides
    whether a push is accepted or discarded as stale.

    Naive timestamps are assumed UTC (that is what the client writes via
    `new Date().toISOString()`); without this, comparing naive against aware
    raises TypeError and would take out the whole request.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        # Python 3.9's fromisoformat only accepts 3- or 6-digit fractional
        # seconds. JS `toISOString()` always emits 3, but a hand-built or
        # third-party timestamp may not — and returning None there would
        # silently make a perfectly valid push LOSE its comparison, which is
        # exactly the class of failure this function exists to prevent. Pad
        # (or trim) the fraction to 6 digits before parsing.
        match = _FRACTION_RE.search(text)
        if match:
            digits = match.group(1)
            if len(digits) != 6:
                padded = (digits + "000000")[:6]
                text = text[: match.start(1)] + padded + text[match.end(1):]
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _profile_map(profiles) -> dict:
    out = {}
    for p in profiles or []:
        if isinstance(p, dict) and p.get("id"):
            out[p["id"]] = p
    return out


def merge_growth_state(stored_state: dict, incoming_state: dict, *,
                       tombstones: set,
                       allow_new_profile_ids: bool) -> dict:
    """Merge `incoming_state` into `stored_state`. Returns a NEW state dict.

    Rules:
      · deletion only via tombstone; a stored profile absent from the push is
        RETAINED
      · tombstoned ids are removed from both sides and can never be recreated
      · same id on both sides -> the whole body with the NEWER per-profile
        `updatedAt` wins; ties keep the STORED body (a tie must not flip
        server state, and the two bodies' ids are identical so the id cannot
        break the tie)
      · a body missing/with an unparseable timestamp can never displace a
        stored body that has a valid one
      · `ledger`, `ledgerBase` and every derived field always come from the
        SAME winning body — mixing them across devices folds to nonsense
      · canonical fields (id/name/profileName) are restored from the stored
        body afterwards, so a stale push can contribute a newer body WITHOUT
        reverting a canonical rename
      · new ids are inserted only when `allow_new_profile_ids`
      · `activeProfileId` is accepted only if it survives; else repaired
      · root keys have explicit rules; unknown keys keep the STORED value

    Returns a dict with the merged state plus bookkeeping:
        {"state", "rejected_profile_ids", "accepted_profile_ids",
         "canonical_profile_fields", "tie_conflicts"}
    """
    stored_state = stored_state or {}
    incoming_state = incoming_state or {}

    stored_profiles = _profile_map(stored_state.get("profiles"))
    incoming_profiles = _profile_map(incoming_state.get("profiles"))

    # Tombstones win over everything, on both sides, before any comparison.
    for pid in list(stored_profiles):
        if pid in tombstones:
            del stored_profiles[pid]
    for pid in list(incoming_profiles):
        if pid in tombstones:
            del incoming_profiles[pid]

    merged: dict = {}
    rejected_ids: list = []
    accepted_ids: list = []
    canonical_fields: list = []
    tie_conflicts: list = []

    # Post-audit fix (Sep 2026): the root-timestamp fallback below is only
    # a legitimate freshness signal from a client that NEVER stamps
    # individual profiles — for such a client, a per-profile miss is the
    # normal case, and root is the only signal it ever sends. It stops
    # being legitimate the moment a side ALSO sends at least one genuinely
    # stamped profile: at that point the client is capable of per-profile
    # stamps, so a specific profile missing one is a real anomaly (a bug,
    # a malformed body, a field silently dropped somewhere in transit) —
    # exactly the case §4.5b's own rule already covers ("a malformed or
    # missing incoming timestamp can never overwrite a valid stored body"),
    # and using the root timestamp as a proxy there let an unrelated
    # root-level bump make a STALE profile body look newer than a valid
    # stored one purely because something else in the same push advanced
    # the root clock. Computed ONCE per side, not per profile, so
    # `person`'s equivalent all-or-nothing check further below can reuse
    # exactly the same signal.
    stored_has_any_profile_stamp = any(
        parse_timestamp(p.get("updatedAt")) is not None for p in stored_profiles.values()
    )
    incoming_has_any_profile_stamp = any(
        parse_timestamp(p.get("updatedAt")) is not None for p in incoming_profiles.values()
    )

    # 1. Every stored profile survives unless tombstoned (I1).
    for pid, stored_body in stored_profiles.items():
        incoming_body = incoming_profiles.get(pid)
        if incoming_body is None:
            merged[pid] = stored_body
            continue

        # Per-profile timestamp preferred. The ROOT timestamp is used as a
        # fallback ONLY for a side that stamps NO profile at all on this
        # push (see the module-level comment above) — never for a side
        # that stamps some profiles but happens to omit THIS one, which is
        # treated as missing/malformed instead, per §4.5b.
        stored_own_at = parse_timestamp(stored_body.get("updatedAt"))
        stored_at = stored_own_at if stored_own_at is not None else (
            parse_timestamp(stored_state.get("updatedAt")) if not stored_has_any_profile_stamp else None
        )
        incoming_own_at = parse_timestamp(incoming_body.get("updatedAt"))
        incoming_at = incoming_own_at if incoming_own_at is not None else (
            parse_timestamp(incoming_state.get("updatedAt")) if not incoming_has_any_profile_stamp else None
        )

        if incoming_at is None and stored_at is None:
            # NEITHER side is stamped — an older client that sends no
            # timestamps at all. There is no staleness claim to weigh, and
            # those builds depend on their pushes still applying, so the
            # incoming body wins. (Long-standing behaviour; see
            # tests/test_batch_c.py "an unstamped push ... still applies".)
            winner = incoming_body
        elif incoming_at is None:
            # Incoming is unstamped/malformed but the STORED side has a real
            # timestamp: that is a demonstrable staleness signal, so the
            # stored body holds.
            winner = stored_body
        elif stored_at is None:
            winner = incoming_body
        elif incoming_at > stored_at:
            winner = incoming_body
        elif incoming_at < stored_at:
            winner = stored_body
        else:
            # Exact tie: stored wins, so a tie can never flip server state.
            winner = stored_body
            if incoming_body != stored_body:
                tie_conflicts.append(pid)

        if winner is incoming_body:
            # Adopt the newer body wholesale — its ledger/ledgerBase and
            # derived fields must stay internally consistent, so they always
            # travel together.
            body = dict(incoming_body)

            # I2: a name the server has established with server-side
            # knowledge of uniqueness — at bootstrap seed
            # (ensure_growth_state), canonical creation (POST
            # /profile/profiles), or explicit rename (PATCH
            # /profile/profiles/{id}) — is protected from being overwritten
            # by a blob. A stale device carrying an old name is
            # indistinguishable from a deliberate rename-back, so the
            # server must not let it silently revert what it already
            # established, AND (§2.4) must not let a stale blob rename a
            # profile to something colliding with another live one — the
            # generic push path runs no uniqueness check at all, unlike the
            # canonical endpoints above.
            #
            # Post-audit fix (Sep 2026): this used to gate ONLY on an
            # explicit rename having happened — a profile's ORIGINAL name
            # (bootstrap or create-time, the overwhelmingly common case
            # since most users never rename) had no protection until its
            # first rename, contradicting §4.5c's unconditional wording and
            # letting a stale push silently rename it with zero uniqueness
            # check. Fixed at the SOURCE (ensure_growth_state and
            # create_growth_profile now set CANONICAL_NAME_FLAG at
            # creation) rather than here — every profile the server ever
            # creates already had its name checked for uniqueness, so there
            # is no remaining case where a profile legitimately has NO
            # canonical name on record. The "ordinary offline rename" path
            # this comment used to describe as intentional was the bug
            # itself — see the correction in
            # tests/test_growth_profile_assignment.py.
            if stored_body.get(CANONICAL_NAME_FLAG):
                changed = {}
                for field in CANONICAL_PROFILE_FIELDS:
                    if field in stored_body:
                        if body.get(field) != stored_body.get(field):
                            changed[field] = stored_body.get(field)
                        body[field] = stored_body.get(field)
                body[CANONICAL_NAME_FLAG] = True
                if changed:
                    canonical_fields.append({"id": pid, "fields": changed})
            else:
                # `id` is never client-assignable even so — it is the merge
                # key itself, and letting a body rewrite it would detach the
                # profile from every book pointing at it.
                body["id"] = pid
            merged[pid] = body
            accepted_ids.append(pid)
        else:
            merged[pid] = stored_body

    # 2. Ids the server has never seen.
    #
    # BOOTSTRAP EXCEPTION: a user with no stored profiles at all is pushing
    # their onboarding profile for the first time. Local-first onboarding
    # creates that profile on the device before any account exists, so
    # rejecting it would leave every new FREE user unable to sync the one
    # profile they are entitled to — and the client cannot delete it either
    # (deleteProfile refuses the last one), so they would be stuck forever.
    # Exactly ONE is allowed through, so this cannot be used to create
    # several profiles without entitlement.
    bootstrap_allowance = 0 if stored_profiles else 1

    for pid, incoming_body in incoming_profiles.items():
        if pid in merged:
            continue
        if allow_new_profile_ids or bootstrap_allowance > 0:
            if not allow_new_profile_ids:
                bootstrap_allowance -= 1
            body = dict(incoming_body)
            # Post-audit fix (Sep 2026): this is a THIRD place (alongside
            # ensure_growth_state's bootstrap seed and create_growth_
            # profile) where a profile's name is established with
            # server-side knowledge of uniqueness — the bootstrap
            # allowance above is capped at exactly one profile against an
            # otherwise-empty stored set, so there is trivially nothing to
            # collide with. Mark it canonical immediately so growth_merge's
            # own I2 defence (this function, above) protects THIS name
            # on every later merge — without this, a user's very first,
            # never-yet-renamed profile (bootstrapped through the generic
            # PUT /profile/growth rather than POST /profile/profiles —
            # e.g. an app version that pushes onboarding state directly)
            # had no protection until an explicit rename.
            body[CANONICAL_NAME_FLAG] = True
            merged[pid] = body
            accepted_ids.append(pid)
        else:
            rejected_ids.append(pid)

    # Preserve the incoming order where possible (the client's own ordering
    # is meaningful for display), appending anything only the server knows.
    ordered = []
    seen = set()
    for p in incoming_state.get("profiles") or []:
        pid = p.get("id") if isinstance(p, dict) else None
        if pid and pid in merged and pid not in seen:
            ordered.append(merged[pid])
            seen.add(pid)
    for p in stored_state.get("profiles") or []:
        pid = p.get("id") if isinstance(p, dict) else None
        if pid and pid in merged and pid not in seen:
            ordered.append(merged[pid])
            seen.add(pid)

    # ── Root keys: one explicit rule each ────────────────────────────────
    # Unknown/future keys keep the STORED value, so a field a newer client
    # introduced is not silently dropped by an older client that omits it.
    #
    # An unknown key the server has NEVER seen is adopted from the push,
    # otherwise a field a newer client introduces could never be stored at
    # all: it would be dropped on the first push (nothing stored yet) and
    # dropped again on every push after that (still nothing stored). Once
    # stored, the stored value wins — an older client omitting the key
    # cannot erase it.
    result = dict(stored_state)
    for key, value in incoming_state.items():
        if key not in result and key not in _EXPLICIT_ROOT_KEYS:
            result[key] = value
    result["profiles"] = ordered

    # updatedAt: the maximum accepted VALID timestamp.
    stored_root_at = parse_timestamp(stored_state.get("updatedAt"))
    incoming_root_at = parse_timestamp(incoming_state.get("updatedAt"))
    if incoming_root_at and (not stored_root_at or incoming_root_at > stored_root_at):
        result["updatedAt"] = incoming_state.get("updatedAt")
    elif stored_state.get("updatedAt") is not None:
        result["updatedAt"] = stored_state.get("updatedAt")

    # person: the newer valid root value wins.
    #
    # An UNSTAMPED push (an older client that sends no `updatedAt` FIELD at
    # all) must still apply — that is long-standing behaviour those builds
    # depend on, and it is unambiguous: with no timestamp claimed on either
    # side there is no staleness signal to weigh. Only a push that is
    # DEMONSTRABLY older (both sides stamped, incoming < stored) is
    # ignored.
    #
    # Post-audit fix (Sep 2026): distinguishes "the field is ABSENT" (old
    # client, tolerate it — unchanged from the original behaviour) from
    # "the field is PRESENT but unparseable" (a genuine anomaly — a bug or
    # a malformed body, per §4.5b's "malformed... can never overwrite a
    # valid stored value"). `parse_timestamp(x) is None` cannot tell these
    # apart on its own (missing and malformed both parse to None), so the
    # raw field's presence is checked directly. The is-None check below was
    # previously true for BOTH cases, which let a genuinely malformed (but
    # present) incoming updatedAt silently overwrite a validly-timestamped
    # stored person — the actual bug; a wholly absent field was never the
    # problem and must keep applying exactly as before.
    incoming_had_updated_at_field = "updatedAt" in incoming_state
    if "person" in incoming_state:
        if incoming_root_at is None and incoming_had_updated_at_field and stored_root_at is not None:
            pass  # incoming's updatedAt is PRESENT but malformed; stored is valid -> stored wins
        elif incoming_root_at and stored_root_at:
            if incoming_root_at >= stored_root_at:
                result["person"] = incoming_state.get("person")
        else:
            # Either the field is genuinely absent on the incoming side
            # (old client — apply, per the long-standing rule above), or
            # neither side has a valid timestamp at all.
            result["person"] = incoming_state.get("person")

    # activeProfileId: accepted only when it points at a surviving profile,
    # otherwise repaired deterministically to the first survivor.
    live_ids = [p.get("id") for p in ordered]
    candidate = incoming_state.get("activeProfileId") or stored_state.get("activeProfileId")
    if candidate not in live_ids:
        candidate = live_ids[0] if live_ids else None
    result["activeProfileId"] = candidate

    # deletedProfileIds is union-only and owned by the caller (it persists to
    # its own column); never carried inside growth_state.
    result.pop("deletedProfileIds", None)

    return {
        "state": result,
        "rejected_profile_ids": rejected_ids,
        "accepted_profile_ids": accepted_ids,
        "canonical_profile_fields": canonical_fields,
        "tie_conflicts": tie_conflicts,
    }
