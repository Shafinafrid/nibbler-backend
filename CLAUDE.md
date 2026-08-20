# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The FastAPI backend for Nibbler (the "daily learning bite" mobile app in `../nibbler`). Python + SQLAlchemy + PostgreSQL, deployed on **Railway** (auto-deploys from GitHub; `railway.toml` + `Procfile` both start `uvicorn main:app`). The mobile app authenticates with Firebase and sends its Firebase ID token as a Bearer token to every endpoint here.

## Commands

```bash
pip install -r requirements.txt
uvicorn main:app --reload          # local dev server on :8000
```

Env vars live in `.env` (see `.env.example`): Postgres URL, **LLM routing + one credential block per provider** (see below), Firebase Admin service-account fields, AWS S3, Pinecone, Voyage AI, Expo push token, Mixpanel token. **`.env` contains real production secrets — never print or commit it.** There is no linter or type-checker configured; `tests/` holds runnable scripts (see Tests).

## Architecture

```
main.py                  → app factory, CORS (getnibbler.com allowlist), rate limiter, routers, lifespan
app/config.py            → pydantic-settings Settings (all env vars + product constants)
app/database.py          → engine, SessionLocal, create_tables() + manual migrations
app/middleware/auth.py   → Firebase Admin token verification, get_current_user
app/models/              → users, profiles, library_items, daily_bites+saved_bites, streaks, push_tokens
app/schemas/             → pydantic request/response models
app/routers/             → auth, profile, library, bites, streak, notifications, connect, support, revenuecat
app/services/            → llm/ (text generation), embedding_service, s3_service, url_safety, notification_service, mixpanel_service, sheets_service, email_service
app/services/llm/        → LLMService boundary + router + luna/haiku/qwen adapters (see below)
app/rate_limit.py        → slowapi limiter keyed by Firebase uid (IP pre-auth)
```

### Text generation is provider-neutral (August 2026)
All four text workflows — aspiration interpretation, Wisdom decks, Story metadata, Connect chat — go through **`LLMService`** in `app/services/llm/`. `ClaudeService` is gone; nothing outside that package imports a provider SDK.

**Three interchangeable providers.** Switching between them is a Railway variable change + restart, never a code change:

| | Provider | Model | Notes |
|---|---|---|---|
| default | **Luna** | `gpt-5.6-luna` | OpenAI Responses API, strict Structured Outputs, **reasoning effort capped at `low`, mode pinned to `standard`**. ⚠️ Never send the bare `gpt-5.6` alias — it routes to Sol, a pricier model. |
| fallback 1 | **Haiku** | `claude-haiku-4-5` | Anthropic, forced tool-use for JSON + prompt caching on the stable system block. **Sonnet is not used and the adapter refuses to run it.** |
| fallback 2 | **Qwen** | `Qwen3-14B` (Q4_K_M) | Configurable OpenAI-compatible HTTPS endpoint on the founder's MacBook. Qwen**3.5 has no 14B** — verified 2026-08-02. See `docs/QWEN_LOCAL_SETUP.md`. |

- `LLM_ROUTING_MODE=single` calls **only** `LLM_ACTIVE_PROVIDER` — used to judge one model's quality without a fallback silently substituting another's work. `fallback` walks `LLM_FALLBACK_ORDER` (default `luna,haiku,qwen`), stopping at the first response that passes schema **and** semantic validation.
- **Subscription tier does not pick a model.** Free, trial and premium all use the configured routing. The old `claude_model_free`/`claude_model_paid` split is dead (the two settings remain declared but unread, purely so an existing Railway deploy still boots — delete them from Railway, then from `config.py`).
- Fallback is **bounded**: each provider at most once per request, plus one same-provider retry reserved for malformed/invalid output. **Safety refusals, our own bad requests, adapter bugs (`INTERNAL`) and unclassified failures (`UNKNOWN`) all stop the chain** — shopping for a model that will comply is not a feature, and rediscovering our own `TypeError` at two more providers is how a bug becomes a bill.
- **Both SDKs default to `max_retries=2`.** All three adapters set it to **0**. Left alone, one logged attempt could be three billed HTTP calls and a three-provider chain up to seven, none of them visible in telemetry. Retry policy belongs to the router, which counts what it spends.
- The circuit breaker is **module-level** (`router.get_shared_breaker()`), not per-router. `LLMService()` is constructed per request in three places, so a breaker owned by the router died with the request that opened it and protected nothing. Still process-local — see `circuit.py`.
- **Every JSON response is validated locally** against the same schema (`jsonschema_lite.py`), on every provider, before semantic validation. Supplying a schema and receiving one back are different events: Haiku treats it as a tool hint, Qwen's grammar coverage depends on the llama-server build, and all three have a prose fallback path that bypasses structured output.
- **Pull-quotes are checked against the source.** `validate_wisdom(..., source_chunks=…)` verifies every `highlight` actually occurs in the retrieved excerpts (whitespace- and quote-normalised, quotes under 25 chars skipped). A fabricated pull-quote is the product's core promise breaking silently, and a prompt instruction is not a guarantee.
- `validate_llm_settings()` runs in `main.py`'s lifespan: an unknown mode, duplicate/disabled provider, missing credential, or `>low` reasoning is a **boot failure with a readable message**, not a 502 later. It makes no network or paid call.
- **Railway cannot reach a MacBook's localhost** — `127.0.0.1` inside the container *is* the container. `QWEN_BASE_URL` must be the authenticated HTTPS tunnel URL in production. A **public plain-HTTP URL is a boot error** (excerpts and the bearer token would travel in clear text), and so is a **loopback URL when `APP_ENV=production`** — outside production it is just the normal local-dev warning.
- **`QWEN_CONTEXT_SIZE` must be ≥ 24576 and defaults to 32768.** A 15-minute Wisdom deck needs ~16,000 tokens (8,000 in + 8,000 reserved out); the minimum is derived from those constants in `router.py` with a 1.5× margin, because an over-long prompt does not error — llama-server drops its *start*, where the instructions are. Keep it in sync with llama-server's `--ctx-size`.
- Model ids are pinned by **exact match**, not prefix — `ALLOWED_LUNA_MODELS` in `luna.py` and `ALLOWED_HAIKU_MODELS` in `haiku.py`, imported by `router.py` so **startup and runtime share one list**. A prefix test accepted `gpt-5.6-luna-preview` and `claude-haiku-3`. The adapters re-check on every call, so a deployment that skips startup validation still cannot reach an unapproved model.
- Telemetry carries a **`request_id`** on every event of one logical request (attempts, fallback, summary), per-attempt latency (not cumulative), and `providers_tried` — `fell_back` is true only when a *different* provider answered, so a same-provider retry does not masquerade as a fallback.
- Telemetry: `llm_attempt` / `llm_usage` / `llm_fallback` structured log lines carry tokens (including Luna's hidden reasoning tokens, which bill as output), latency, error category, final provider and estimated cost. **They never contain prompts, excerpts, chat history, card bodies or keys.**
- **AI image generation stays disabled.** `app/services/image_gen.py` is imported by nothing (it would not even import — it references a `settings` symbol `config.py` does not export), and `image_generation_enabled` now defaults to **False**. `OPENAI_LLM_API_KEY` is a separate setting from `OPENAI_API_KEY` precisely so configuring Luna cannot switch images on.

### Nibble card images come only from the user's own book (August 2026)
A card may carry a figure the **author** put in the book — never a generated one. Most cards have none, and a text-only card is the expected, correct outcome. Tier is irrelevant: free, trial and premium behave identically.

```
app/services/image_extract.py           → extract figures from PDF/EPUB at upload, with provenance
app/services/image_select.py            → shortlist, then re-validate the model's choice server-side
GET /library/{item_id}/images/{img_id}  → authenticated, owner+book-scoped, mints a 1-hour view URL
```

- **Extraction runs after text is safely indexed** and never raises: Pillow missing, a corrupt figure, a dead S3, a malformed OPF all end with a normal text-only book. Existing library items are **not** reprocessed — a book uploaded before this has `images = None` and keeps working.
- **Rejection is most of the work**: sub-320x220 decoration, >6:1 slivers, **blank/solid panels** (judged by histogram variance, so black separators are caught as well as white), the cover, name-matched furniture (logo/colophon/ornament…), byte-identical duplicates, *visually* identical duplicates (8x8 average hash, Hamming ≤ 4), and anything repeated on more than 3 pages.
- **The 60-image cap is applied AFTER deduplication**, and separately from a `MAX_SCAN` (400) and a `MAX_TOTAL_BYTES` (64 MiB) budget. Capping during collection let sixty copies of a logo consume the quota and hide the real figure on page 61; `MAX_IMAGES × MAX_BYTES` alone would also have allowed 480 MiB in memory on one upload. The byte budget is checked **before** an image is accepted (checking after subtracting allowed a 72 MiB peak), and `page.images` is iterated lazily — `list()` would materialise a whole pathological page before any budget applied.
- **A PDF cover is recognised without geometry.** pypdf exposes an image's *pixel* size but not the rectangle it is drawn into, so "does it fill the page" cannot be measured (comparing 600×400 px against 595×842 pt compares nothing). Instead: page one + a single image + under 25 words + no figure caption.
- **`visual_kind` and blankness use the full greyscale histogram**, not a downsample. Smoothing resamples average detail away (a photograph measures as flat); NEAREST aliases on regular patterns (a noisy figure measured as blank). Distinct **raw levels**, not 8-wide buckets — a high-key photograph living between 190 and 255 occupies ~7 buckets and was classified as a diagram.
- **Provenance** per candidate: opaque id (salted with the book), owner, book, private S3 key, checksum, MIME, source order, page (PDF) or spine index + chapter (EPUB), nearby text, caption, alt text, dimensions, `visual` (diagram → `contain`, photo → `cover`), `position`, and `position_basis`.
- **`position` is measured in WORDS** where the text allows, because `story_progress` is a word offset. A page or spine fraction is not comparable — a figure on page 12 of 300 is not "4% through" when 40 pages are front matter — so `position_basis` records the unit and **Story refuses anything that is not `"words"`** rather than converting it.
- **A position is the END of the unit containing the figure, not its start.** Neither format reports where *on* a page a figure sits, so the safe assumption is the furthest point it could be. Using the start gave a figure at the bottom of a long page one a position of `0.0`, and it was offered to a reader 10% in.
- **The model never sees a key, URL or filename** — only `{id, description}` from a bounded shortlist (≤8), and it may name at most one id per card. `image_select.validate_selection` then re-checks existence, ownership, book, shortlist membership, Story position **and relevance to that specific card** from the stored rows. Shortlist membership is per *deck*, so without the per-card check a habit-loop diagram was accepted on the same deck's marine-biology card.
- **Story never looks ahead.** Candidates past the reader's position are filtered out before the model sees them *and* rejected again afterwards.
- **Cards persist the API path, not a presigned URL** (`/library/{item}/images/{id}`). Book-scoped because a checksum-only id collided across books. Presigned links die in an hour; a nibble is replayed months later, so refreshing is just calling the endpoint again. It returns **JSON, not a 307** — a redirect would have iOS forward the Firebase bearer token to Amazon.
- **Cleanup is ownership-scoped and failure-isolated.** Deleting a book removes its figures; deleting an account removes every book's figures (including books whose source file was never archived). Keys are prefix-checked, and **each delete is individually wrapped** so one transient error cannot abandon the objects after it. If the database commit that records the rows fails, the objects already uploaded are **deleted** rather than orphaned.
- **Nothing about images can fail a session.** Extraction, shortlisting (`safe_shortlist`), prompt building (`safe_prompt`), selection and attachment are each wrapped; a malformed `images` blob of any shape yields a valid text-only nibble.

### Tests
`tests/` holds runnable persistence suites — plain scripts, no pytest/CI: `for t in tests/test_*.py; do .venv/bin/python "$t"; done`. Each uses a throwaway SQLite DB with its own env vars, so the real `.env` is never loaded. **SQLite doesn't enforce FKs by default**, which is why `DELETE /library/{id}` deletes child rows explicitly instead of relying on `ON DELETE CASCADE`.

### Migrations are manual, not Alembic
Alembic is in requirements but **not used**. `create_tables()` runs `Base.metadata.create_all()` on startup, then `_run_migrations()` in `app/database.py` executes a hand-maintained list of `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements. **When you add a column to a model, you must also append the matching ALTER to that list** so Railway applies it on deploy.

### Deployment readiness (Task 19, Aug 2026)
Before this task, a failed migration statement was only logged — `create_tables()` always returned normally, so a deploy with a genuinely broken/missing required column still reported "started successfully" on `/health` and only 500'd hours later on whichever endpoint touched it first (a false-green deployment). Fixed with a fail-closed contract, **Postgres only**:
- `_run_migrations()` and `_run_task2_required_migrations_and_backfill()` both now run their whole statement sequence behind a **session-level Postgres advisory lock** (`MIGRATIONS_ADVISORY_LOCK_KEY` / `TASK2_ADVISORY_LOCK_KEY`, distinct keys) so a rolling deploy's overlapping old+new workers can't race the same ALTER — and if any statement fails, `create_tables()` **raises**, which (via `main.py`'s `lifespan()`, which has no try/except around it) stops the app from ever accepting traffic. On **SQLite** (local/test) this stays lenient exactly as before — most statements there are inherently Postgres-only syntax, and several test suites depend on the real dialect-agnostic ones (CREATE INDEX/UPDATE) still running without raising.
- `verify_required_schema()` (`app/database.py`) then re-derives the EFFECTIVE schema from `information_schema`/`pg_catalog` (SQLite: `PRAGMA table_info`/`sqlite_master`) and compares against a short registry (`REQUIRED_TABLES`/`REQUIRED_COLUMNS`/`REQUIRED_PG_CONSTRAINTS`) — because a migration statement not raising is not proof the column is actually there with the right name, just proof that one SQL call didn't throw. A mismatch also raises and blocks startup. **Extend this registry, don't bypass it**, whenever a future task adds another constraint whose absence would silently break an integrity guarantee (an idempotency key, a dedupe constraint, a new required table).
- `GET /ready` (main.py) is a **new, separate** endpoint from `/health` — `/health` still only proves the process is alive (always 200). `/ready` additionally proves the database is reachable *right now* (live `SELECT 1`, checked per-call — connectivity can be lost after a clean boot) AND the schema was verified at boot (`_boot_schema_verified`, cached — the schema can't change at runtime, so this isn't re-derived on every poll). Returns 503 when either check fails, with a small JSON body (`status`/`db_reachable`/`schema_verified`/truncated `db_error`) — never a DB URL, credential, or query text.
- **Not yet done, deliberately** (Task 19's own boundary — implementation only, no production/Railway change): `railway.toml`'s `healthcheckPath` is still `/health`. Pointing Railway at `/ready` instead is the natural next step (so a deploy Railway calls "healthy" actually means ready-for-traffic, not just alive) — a one-line change (`healthcheckPath = "/ready"`), left for Shafin to apply alongside an actual deploy, since it changes Railway's real rollout gating behavior.
- Proven two ways: `tests/test_task19_deployment_readiness.py` (SQLite, dialect-independent logic + the HTTP contract) and `tests/test_task19_pg_harness.py` (a real disposable Postgres cluster — the fail-hard raise, the advisory lock actually releasing after a failure, two real connections genuinely serializing on the lock, and schema verification catching a dropped constraint/table via real `pg_catalog`/`information_schema` queries — same pattern as Task 2's own `test_task2_pg_harness.py`).

### Auth flow
`get_current_user` verifies the Firebase ID token and **auto-creates a User row** (`id` = Firebase UID) if none exists — there is no separate signup endpoint. `DELETE /auth/me` is the GDPR erasure path: S3 files → Pinecone namespace → Postgres (CASCADE) → Firebase account → Mixpanel event; partial failures log but don't block.

### Content pipeline (library → embeddings → bites)
1. `POST /library/` (text/note), `/library/upload-pdf` (S3 upload, bucket `nibbler-user-files`, eu-north-1), or `/library/add-url` (requests + BeautifulSoup scrape).
2. Each add schedules a FastAPI `BackgroundTasks` job that extracts text via `app/services/text_extract.py` (**structure-preserving**: pypdf `extraction_mode="layout"` → indent/blank-line paragraph rebuilding, hyphenation repair, running-head removal, paragraphs stitched across page breaks; EPUBs walk block-level tags in spine order. Story mode serves this text to the reader verbatim, so never flatten it — joining pages with `" "` is what made every nibble one run-on block), chunks it (tiktoken, 500 tokens / 50 overlap), embeds via **Voyage AI `voyage-3-lite` (512 dims)**, and upserts to **Pinecone** index `nibbler-content` under a **per-user namespace** with an `embedder` metadata stamp. Result recorded on the row (`processed`, `chunk_count`, `processing_error`). Mock embeddings are used ONLY when no Voyage key is configured (keyless dev); if a key is set and Voyage fails, `EmbeddingError` is raised and recorded on the row — never silently indexed (a July-2026 bug did exactly that and poisoned Connect's goal-match; `/connect/insights` detects legacy mock vectors and re-embeds in the background). Without Pinecone, indexing silently no-ops. Voyage account has a payment method attached (2026-07-20) — real rate limits apply, not the old 3 RPM / 10K TPM keyless-tier cap. `_embed_batch_with_backoff` in `embedding_service.py` still retries 429s (background ingestion ~5min budget, live request paths ~6s) as a safety net.
3. `POST /bites/session` generates the per-book card-deck session on demand (cached per user/item/day, `client_date`-aware): retrieval query from the transmitted growth profile, top-K chunks from Pinecone, strict-JSON deck from whichever provider the LLM routing config selects (see "Text generation is provider-neutral" above — tier does NOT change the model). Daily caps enforced: 1 new generation/day free, 3/day premium (`config.py`). Streaks are written ONLY by `POST /streak/checkin`. (The legacy `GET /bites/today` + chat-onboarding endpoints were removed July 2026.) Uploads: **50 MB** PDF/EPUB cap (`max_pdf_upload_mb`, raised from 20 on 2026-07-25; the app checks the same number at pick time — keep `MAX_UPLOAD_MB` in `nibbler/src/screens/UploadScreen.js` in sync), SSRF-guarded URL fetch (`app/services/url_safety.py`), Voyage embeds batched 128/call.

### Notifications
`notification_service` runs an **APScheduler cron every 5 minutes** that pushes "Your daily bite is ready" via Expo's push API to every token whose `notification_hour` + `notification_minute` (stored **in UTC**, minutes snapped to 5-min slots; the app converts from local time) matches the current slot. Tokens are registered/updated via `/notifications/*`. Do NOT run uvicorn with multiple workers — the scheduler would fire once per process (duplicate pushes).

### Durable delivery (Task 20, Aug 2026)
The 5-min on-time passes above (`_prepare_user_nibbles`/`_notify_delivery_slot`/`_notify_streak_alert_slot`) are unchanged and still the fast happy path — but before this task they were the ONLY record that a user's daily cycle existed: a restart between "due" and "pushed" silently lost that day's cycle until the exact same slot recurred 24h later, and Expo's per-message ticket (success/failure/unknown) was parsed then discarded.
- **`delivery_cycles`** (`app/models/delivery.py`) is the durable ledger — one row per `(user_id, cycle_date)` (real unique constraint, the whole idempotency anchor), an explicit state machine (`due → held_unread/generation-claimed → push_pending → push_submitted_unknown → completed`, plus `retryable_failure`/`terminal_failure`/`window_expired`/`superseded`), and a `claimed_by`/`claimed_until` lease pair matching `CleanupTask`/`ChatTurn`'s established idiom.
- **`app/services/delivery_lifecycle.py`**'s `reconcile_delivery_cycles` runs every tick, additively, from `_run_delivery_cycle` — it resumes/finishes anything a restart, crash or long stall left incomplete, reusing the EXACT existing unread-hold/fair-rotation/curiosity-hook/`generate_session_for_item`/`send_push_messages` functions rather than reimplementing any of them.
- **Bounded catch-up, not open-ended**: `GENERATION_CATCHUP_WINDOW`/`PUSH_CATCHUP_WINDOW` = 6h from `due_at` (fixed at creation, never rewritten); past that, `window_expired` — a bounded miss, not a retry-forever. `MAX_ATTEMPTS = 4` bounds retry count independently. Streak alerts get their own, much tighter `STREAK_ALERT_CATCHUP_WINDOW` (20 min, guarded by `Streak.last_alert_sent_date` idempotency) — a stale "ends in one hour" alert is worse than a missed one.
- **Uncertain push outcomes never trigger regeneration** — a `push_submitted_unknown` cycle retries only the send, bounded, never calls back into generation. Expo's `DeviceNotRegistered` is dead-lettered immediately (no retry burned on an unfixable error); other errors/timeouts retry bounded.
- **Claim-lease correctness (fixed after an independent audit found both bugs pre-commit)**: `_try_claim`'s lease is anchored to a FRESH `datetime.utcnow()` read at claim time, never a stale batch-start timestamp (a `RECONCILE_BATCH_SIZE=25`-row sweep could otherwise compute an already-expired lease for a row claimed late in the batch). The real scheduler's `worker_id` (`notification_service._WORKER_ID`) is generated once per OS process (hostname+pid+uuid) — a shared literal here would make the lease's cross-process protection a complete no-op.
- **Readiness coordination (Task 19)**: `delivery_cycles` + its unique constraint are in `app/database.py`'s `REQUIRED_TABLES`/`REQUIRED_PG_CONSTRAINTS`; `GET /ready` also requires `notification_service.scheduler_initialized()`.
- Proven both ways: `tests/test_task20_durable_delivery.py` (SQLite — the full state machine, restart/concurrency/Expo-outcome/window/streak-alert scenarios) and `tests/test_task20_pg_harness.py` (real disposable Postgres — two genuine OS threads racing the claim lease and the unique-constraint dedup).
- **Not done under this task** (its own explicit boundary): no production migration, Railway change, deployment, or real push/paid-AI call — implementation + tests only, committed but not pushed until reviewed.

### Free vs premium enforcement (implemented July 2026)
- `User.effective_premium` is the single tier source: `is_premium` (manual comps) OR subscription (`premium_until`) OR 7-day signup trial. (The a@a.com/b@b.com dev-email overrides were removed — do not re-add.)
- Subscription sync writes `premium_until`: `POST /webhooks/revenuecat` (shared-secret Authorization header; configure the URL + `REVENUECAT_WEBHOOK_SECRET` in the RC dashboard/Railway) and `POST /auth/sync-premium` (app calls it after purchase/restore; server verifies with RC's REST API — needs `REVENUECAT_SECRET_API_KEY`). Never trust client-claimed premium.
- Free: 3 uploads, 1 new session/day, 7-day history; Connect (chat + insights) is premium-only (403 `premium_required`). Premium: 3 sessions/day, ≤5 active sources (`PATCH /library/{id}/active`).
- Rate limits (slowapi, in-memory): chat 20/hr, session 30/day, uploads 10-20/hr, interpret-aspiration 10/hr/IP.

## Canonical product decisions (July 2026 — override older docs)
- Pricing: **$9.99/mo, $69.99/yr** (correct on all surfaces as of 2026-07-12).
- Library model: uploads are **uncapped**; premium users select **up to 5 "active" sources** (`library_items.is_active` + `PATCH /library/{id}/active`, cap enforced server-side).
- Free tier: 3 uploads, 1 bite/day, 7-day history.

## Remaining gaps (most PRD gaps closed July 2026)
- The growth profile now persists server-side as a JSON blob (`profiles.growth_state`, `PUT /profile/growth`); the legacy chat-interview columns remain on the table but have no write path.
- Quizzes are generated inside sessions and reviewed client-side; there is no standalone quiz/flashcard endpoint or spaced-repetition persistence (post-launch).
- Multiple growth profiles (Phase 2) not started server-side.
- Account deletion does not purge the bug-report mirror rows in the Google Sheet (founder decision pending).

### Prod-DB one-offs already applied (2026-07-16)
Unique indexes are LIVE in production: `uq_daily_bites_user_item_date` on `daily_bites (user_id, library_item_id, date) WHERE library_item_id IS NOT NULL`, and `uq_saved_bites_user_bite` on `saved_bites (user_id, bite_id)`. Both write paths tolerate the IntegrityError (session_service returns the winning row; the save endpoint returns "Already saved"). **Running SQL on prod:** Railway → Postgres service → **Console** tab (a bash shell) → `psql "$DATABASE_URL"` (internal endpoint, no egress fees). The Database→Query tab silently appends `LIMIT` — SELECT-only, DDL/DELETE will syntax-error there.
