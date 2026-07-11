# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The FastAPI backend for Nibbler (the "daily learning bite" mobile app in `../nibbler`). Python + SQLAlchemy + PostgreSQL, deployed on **Railway** (auto-deploys from GitHub; `railway.toml` + `Procfile` both start `uvicorn main:app`). The mobile app authenticates with Firebase and sends its Firebase ID token as a Bearer token to every endpoint here.

## Commands

```bash
pip install -r requirements.txt
uvicorn main:app --reload          # local dev server on :8000
```

Env vars live in `.env` (see `.env.example`): Postgres URL, Claude API key + model names, Firebase Admin service-account fields, AWS S3, Pinecone, Voyage AI, Expo push token, Mixpanel token. **`.env` contains real production secrets — never print or commit it.** There is no test suite, linter, or type-checker configured.

## Architecture

```
main.py                  → app factory, CORS (getnibbler.com allowlist), rate limiter, routers, lifespan
app/config.py            → pydantic-settings Settings (all env vars + product constants)
app/database.py          → engine, SessionLocal, create_tables() + manual migrations
app/middleware/auth.py   → Firebase Admin token verification, get_current_user
app/models/              → users, profiles, library_items, daily_bites+saved_bites, streaks, push_tokens
app/schemas/             → pydantic request/response models
app/routers/             → auth, profile, library, bites, streak, notifications, connect, support, revenuecat
app/services/            → claude, embedding_service, s3_service, url_safety, notification_service, mixpanel_service, sheets_service, email_service
app/rate_limit.py        → slowapi limiter keyed by Firebase uid (IP pre-auth)
```

### Migrations are manual, not Alembic
Alembic is in requirements but **not used**. `create_tables()` runs `Base.metadata.create_all()` on startup, then `_run_migrations()` in `app/database.py` executes a hand-maintained list of `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements. **When you add a column to a model, you must also append the matching ALTER to that list** so Railway applies it on deploy.

### Auth flow
`get_current_user` verifies the Firebase ID token and **auto-creates a User row** (`id` = Firebase UID) if none exists — there is no separate signup endpoint. `DELETE /auth/me` is the GDPR erasure path: S3 files → Pinecone namespace → Postgres (CASCADE) → Firebase account → Mixpanel event; partial failures log but don't block.

### Content pipeline (library → embeddings → bites)
1. `POST /library/` (text/note), `/library/upload-pdf` (S3 upload, bucket `nibbler-user-files`, eu-north-1), or `/library/add-url` (requests + BeautifulSoup scrape).
2. Each add schedules a FastAPI `BackgroundTasks` job that extracts text, chunks it (tiktoken, 500 tokens / 50 overlap), embeds via **Voyage AI `voyage-3-lite` (512 dims)**, and upserts to **Pinecone** index `nibbler-content` under a **per-user namespace**. Result recorded on the row (`processed`, `chunk_count`, `processing_error`). Without a Voyage key, embeddings fall back to a deterministic mock (dev only); without Pinecone, indexing silently no-ops.
3. `POST /bites/session` generates the per-book card-deck session on demand (cached per user/item/day, `client_date`-aware): retrieval query from the transmitted growth profile, top-K chunks from Pinecone, strict-JSON deck from Claude. Free users get `claude-haiku-4-5`, premium `claude-sonnet-4-6`. Daily caps enforced: 1 new generation/day free, 3/day premium (`config.py`). Streaks are written ONLY by `POST /streak/checkin`. (The legacy `GET /bites/today` + chat-onboarding endpoints were removed July 2026.) Uploads: 20 MB PDF cap, SSRF-guarded URL fetch (`app/services/url_safety.py`), Voyage embeds batched 128/call.

### Notifications
`notification_service` runs an **APScheduler cron every 5 minutes** that pushes "Your daily bite is ready" via Expo's push API to every token whose `notification_hour` + `notification_minute` (stored **in UTC**, minutes snapped to 5-min slots; the app converts from local time) matches the current slot. Tokens are registered/updated via `/notifications/*`. Do NOT run uvicorn with multiple workers — the scheduler would fire once per process (duplicate pushes).

### Free vs premium enforcement (implemented July 2026)
- `User.effective_premium` is the single tier source: subscription (`premium_until`) OR 7-day signup trial OR dev-email override.
- Subscription sync writes `premium_until`: `POST /webhooks/revenuecat` (shared-secret Authorization header; configure the URL + `REVENUECAT_WEBHOOK_SECRET` in the RC dashboard/Railway) and `POST /auth/sync-premium` (app calls it after purchase/restore; server verifies with RC's REST API — needs `REVENUECAT_SECRET_API_KEY`). Never trust client-claimed premium.
- Free: 3 uploads, 1 new session/day, 7-day history; Connect (chat + insights) is premium-only (403 `premium_required`). Premium: 3 sessions/day, ≤5 active sources (`PATCH /library/{id}/active`).
- Rate limits (slowapi, in-memory): chat 20/hr, session 30/day, uploads 10-20/hr, interpret-aspiration 10/hr/IP.

## Canonical product decisions (July 2026 — override older docs)
- Pricing: **$9.99/mo, $59.99/yr** (correct on all surfaces as of 2026-07-10).
- Library model: uploads are **uncapped**; premium users select **up to 5 "active" sources** (`library_items.is_active` + `PATCH /library/{id}/active`, cap enforced server-side).
- Free tier: 3 uploads, 1 bite/day, 7-day history.

## Remaining gaps (most PRD gaps closed July 2026)
- The growth profile now persists server-side as a JSON blob (`profiles.growth_state`, `PUT /profile/growth`); the legacy chat-interview columns remain on the table but have no write path.
- Quizzes are generated inside sessions and reviewed client-side; there is no standalone quiz/flashcard endpoint or spaced-repetition persistence (post-launch).
- Multiple growth profiles (Phase 2) not started server-side.
- Unique indexes on `daily_bites (user_id, library_item_id, date)` and `saved_bites (user_id, bite_id)` need one-off manual SQL (the `_run_migrations` pattern can't add constraints); the session handler already tolerates the resulting IntegrityError.
- Account deletion does not purge the bug-report mirror rows in the Google Sheet (founder decision pending).
