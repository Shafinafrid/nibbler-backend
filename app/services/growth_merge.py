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

    # 1. Every stored profile survives unless tombstoned (I1).
    for pid, stored_body in stored_profiles.items():
        incoming_body = incoming_profiles.get(pid)
        if incoming_body is None:
            merged[pid] = stored_body
            continue

        # Per-profile timestamp preferred; fall back to that side's ROOT
        # timestamp when a body carries none.
        #
        # Not every client stamps individual profiles — plenty of pushes
        # carry only a root `updatedAt` for the whole blob. Treating those
        # as "untimestamped" would make every such pair an exact tie, and
        # the tie rule (stored wins) would silently discard a legitimate
        # newer edit, including an ordinary offline rename. The root
        # timestamp is exactly the freshness signal those clients DO send.
        stored_at = (parse_timestamp(stored_body.get("updatedAt"))
                     or parse_timestamp(stored_state.get("updatedAt")))
        incoming_at = (parse_timestamp(incoming_body.get("updatedAt"))
                       or parse_timestamp(incoming_state.get("updatedAt")))

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

            # I2, applied NARROWLY: only a name the server has CANONICALLY
            # established (through PATCH /profile/profiles/{id}) is protected
            # from being overwritten by a blob. That is the case this exists
            # for — a stale device carrying the pre-rename name is
            # indistinguishable from a deliberate rename-back, so the server
            # must not let it silently revert an explicit rename.
            #
            # A profile that has never been renamed canonically keeps the
            # long-standing behaviour: a newer blob may still carry a rename.
            # Freezing those too would break the ordinary offline rename
            # path that predates the canonical endpoint (see
            # tests/test_deletion_tombstones.py), for no safety gain — with
            # no canonical rename on record there is nothing to revert TO.
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
            merged[pid] = incoming_body
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
    # An UNSTAMPED push (an older client that sends no `updatedAt` at all)
    # must still apply — that is long-standing behaviour those builds depend
    # on, and it is unambiguous: with no timestamp on either side there is no
    # staleness claim to weigh. Only a push that is DEMONSTRABLY older (both
    # sides stamped, incoming < stored) is ignored.
    if "person" in incoming_state:
        if incoming_root_at and stored_root_at:
            if incoming_root_at >= stored_root_at:
                result["person"] = incoming_state.get("person")
        else:
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
