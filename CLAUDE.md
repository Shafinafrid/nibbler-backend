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
main.py                  → app factory, CORS (currently allow-all — tighten for prod), routers, lifespan
app/config.py            → pydantic-settings Settings (all env vars + product constants)
app/database.py          → engine, SessionLocal, create_tables() + manual migrations
app/middleware/auth.py   → Firebase Admin token verification, get_current_user
app/models/              → users, profiles, library_items, daily_bites+saved_bites, streaks, push_tokens
app/schemas/             → pydantic request/response models
app/routers/             → auth, profile, library, bites, streak, notifications
app/services/            → claude, bite_generator, embedding_service, s3_service, notification_service, mixpanel_service
```

### Migrations are manual, not Alembic
Alembic is in requirements but **not used**. `create_tables()` runs `Base.metadata.create_all()` on startup, then `_run_migrations()` in `app/database.py` executes a hand-maintained list of `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements. **When you add a column to a model, you must also append the matching ALTER to that list** so Railway applies it on deploy.

### Auth flow
`get_current_user` verifies the Firebase ID token and **auto-creates a User row** (`id` = Firebase UID) if none exists — there is no separate signup endpoint. `DELETE /auth/me` is the GDPR erasure path: S3 files → Pinecone namespace → Postgres (CASCADE) → Firebase account → Mixpanel event; partial failures log but don't block.

### Content pipeline (library → embeddings → bites)
1. `POST /library/` (text/note), `/library/upload-pdf` (S3 upload, bucket `nibbler-user-files`, eu-north-1), or `/library/add-url` (requests + BeautifulSoup scrape).
2. Each add schedules a FastAPI `BackgroundTasks` job that extracts text, chunks it (tiktoken, 500 tokens / 50 overlap), embeds via **Voyage AI `voyage-3-lite` (512 dims)**, and upserts to **Pinecone** index `nibbler-content` under a **per-user namespace**. Result recorded on the row (`processed`, `chunk_count`, `processing_error`). Without a Voyage key, embeddings fall back to a deterministic mock (dev only); without Pinecone, indexing silently no-ops.
3. `GET /bites/today` generates on demand (no pre-generation): `BiteGenerator` builds a search query from the user's profile, pulls top-5 chunks from Pinecone, and asks Claude (`ClaudeService`) for strict-JSON `{title, insight, reflection, action, source, theme}`. Free users get `claude-haiku-4-5`, premium `claude-sonnet-4-6` (models set in config/env). One bite per user per day is stored; the same request updates the streak in a background task.

### Notifications
`notification_service` runs an **APScheduler cron at the top of every hour** that pushes "Your daily bite is ready" via Expo's push API to every token whose `notification_hour` (stored **in UTC, hour granularity**) matches the current hour. Tokens are registered/updated via `/notifications/*`.

### Free vs premium enforcement (current state)
- Free upload cap: `check_upload_limit` blocks free users at `free_upload_limit` (3). Premium has **no cap** here.
- Free bite history is filtered to the last 7 days; premium gets full history.
- `premium_bites_per_day = 3` exists in config but **is not implemented** — everyone gets exactly 1 bite/day.
- `is_premium` on the User row is not updated by any RevenueCat webhook yet — there is no subscription sync or trial logic server-side.

## Canonical product decisions (July 2026 — override older docs)
- Pricing: **$9.99/mo, $59.99/yr** (the PRD's $69.99 is stale).
- Library model: uploads are **uncapped**; premium users select **up to 5 "active" sources** that feed nibble generation, swappable anytime. **Not yet implemented** — there is no "active selection" concept in the schema; adding it is pending work.
- Free tier: 3 uploads, 1 bite/day, 7-day history.

## Known gaps vs the PRD (`../_share-with-claude/docs/MD files/Nibbler_PRD.md`)
- No Story Mode / Wisdom Mode distinction (no content-mode column on `library_items`, no sequential delivery).
- No quiz endpoint, no chat-with-library endpoint (the app's Review and Connect tabs run on demo data). `/profile/onboarding/chat` is a conversational-interview endpoint the app **no longer uses** — the app switched to a local, aspiration-based onboarding.
- The `profiles` table shape (goals/struggles/reading_habits/daily_time/tone_preference) predates the app's local `GrowthProfile` (motivation/goalOrientation/interests/pacing/selfEfficacy/contentMode). The app never syncs its local growth profile to this backend, so bite personalization only works for users who completed the old chat onboarding. Reconciling these two profile shapes is the biggest outstanding integration task.
- Multiple growth profiles (Phase 2) not started server-side.
