# Growth profiles — handoff

**Branch:** `worktree-growth-profiles-server-authoritative` (in
`nibbler-backend/.claude/worktrees/growth-profiles-server-authoritative`)
**Plan:** `~/.claude/plans/foamy-brewing-sun.md`
**Baseline:** `BASELINE_2026-09-03.md`

## Done — backend (phases 0-4, plus the server half of 6)

Two commits:

- `b1ca21b` — stable assignment id, shared resolver, entitlement gate
- `edee817` — ID-aware merge, canonical endpoints, server-side scoring

### Schema (both nullable, both in `verify_required_schema`)
- `library_items.growth_profile_id` — the assignment identity.
  `growth_profile_name` is now a server-DERIVED display snapshot.
- `daily_bites.growth_profile_id` — which profile a goal passage was written
  for.

### New/changed endpoints
| Route | Tier | Notes |
|---|---|---|
| `POST /profile/growth/ensure` | any | idempotent bootstrap |
| `POST /profile/profiles` | **Premium** | authoritative creation |
| `PATCH /profile/profiles/{id}` | any | rename + derived-name refresh, one txn |
| `DELETE /profile/profiles/{id}` | any | tombstone + reassign, one txn |
| `PUT /profile/growth` | any | now MERGES; returns reconciliation payload |
| `PATCH /library/{id}` | **Premium** for assignment | 403 `premium_required` |
| `POST /library/`, `/upload-pdf`, `/add-url` | any | non-entitled: assignment ignored, default substituted (never 403) |
| `POST /connect/insights` | Premium | server-authoritative; ignores client profile |
| `GET /connect/stats/{id}` | Premium | goal passage filtered by provenance |

## Remaining

### Phase 5 — client reconciliation (NOT started)
`PUT /profile/growth` already returns `rejectedProfileIds`,
`acceptedProfileIds`, `canonicalProfileFields`, `effectivePremium`.
`syncOutbox.runOp` (`nibbler/src/data/syncOutbox.js:362`) still DISCARDS that
response and settles the op at `:504-505`. Until it reconciles, a rejected
profile stays in local state and can be re-pushed. Must reconcile BEFORE
settling, and leave the op queued if reconciliation fails.

### Phases 6 (client half) + 7 — the UI, i.e. the original ask (NOT started)
Nothing in `nibbler/` has been touched. All of this is still to do:
- `ProfileScreen.js:1091-1110` — ungate the Growth profiles row
- `GrowthProfilesScreen.js` — gate creation only; Add button must not decide
  while purchase state is loading (it reports `false` while loading, so an
  entitled user would be sent to the paywall)
- `PaywallScreen.js:59-65` — add a `growth_profiles` HEADLINES key, and pass
  `{ feature: 'growth_profiles' }` (NOT `context`; `:92` falls back to the
  generic headline otherwise)
- `LibraryScreen.js:1128-1159` — read-only assignment for free/lapsed
- `UploadScreen.js:74` — `isPro && growthProfiles.length > 1 && wisdom`
- `ConnectScreen.js` — open L1/L2 to free users; the THIS BOOK SERVES block;
  the three-effect split; cache keyed on `resolved_profile_id` +
  `scoring_fingerprint`; `switchInFlightRef` / `analyticsRevision`
- Wire `growth_profile_id` through `api.js:192`'s multipart field list, and
  make pickers compare IDs, not names

### Phase 9/10 — verification + staged rollout
Rollout order matters: backend groundwork is deployed FIRST with enforcement
off, then the client ships, then enforcement is enabled. Enforcement-first
would break shipped clients that still expose assignment to free users.
The `strict_assignment_enforcement` config flag is NOT yet implemented.

## Test state

`bash run_backend_tests.sh results.txt`
(macOS has no `timeout` — do not wrap the python call with it.)

Expected: **2 pre-existing failures, unrelated to growth profiles**, both
present at baseline and documented in `BASELINE_2026-09-03.md`:
- `test_task2_attempt_lifecycle_repro.py` — an intentional RED harness
- `test_task2_pg_harness.py` — `has_held_paid_entitlement` migration/model
  NOT NULL mismatch (worth its own fix: `models/user.py:31` says
  `nullable=False`, `database.py:597` migrates without `NOT NULL`)

Anything else failing is a real regression.

## Bugs the new suite caught (all fixed)

1. A free user's FIRST profile was rejected — the bootstrap exception was
   missing. Would have stranded every new free user permanently, since the
   client refuses to delete the last profile.
2. Unknown/future root keys dropped on first sync.
3. Offline renames discarded when bodies carried no per-profile timestamp.
4. Wholly unstamped pushes from older clients ignored entirely.
5. (Self-inflicted, fixed) returning a Pydantic model from the growth PUT
   broke in-process callers reading `resp.deleted_profile_ids`.

## Pre-existing bug found, NOT fixed

`str(incoming) < str(stored)` in the old growth PUT ordered timestamps with
differing UTC offsets backwards (`11:00+02:00` = 09:00Z was treated as newer
than `10:00Z`). Fixed for this endpoint; if that idiom appears elsewhere it
has the same flaw.
