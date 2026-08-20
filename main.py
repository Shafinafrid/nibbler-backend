from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from slowapi.errors import RateLimitExceeded
from app.database import create_tables, SessionLocal
from app.rate_limit import limiter
from app.routers import auth, profile, library, bites, streak
from app.routers import notifications, connect, support, revenuecat, sync
from app.services.notification_service import start_scheduler, stop_scheduler
from app.services.llm import validate_llm_settings, configure_llm_telemetry_logging
from app.config import get_settings

import logging

logger = logging.getLogger(__name__)
settings = get_settings()


def _db_factory():
    return SessionLocal()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    # First, always: a preloaded/forked worker must run this ITSELF rather
    # than relying on whatever module-import time did (a fork inherits an
    # already-configured logger, but still needs to announce ITS OWN PID —
    # see configure_llm_telemetry_logging's docstring). Before settings
    # validation, database init and the scheduler, and before any route can
    # receive a request.
    configure_llm_telemetry_logging()
    # Fail loudly and early on an unusable provider configuration: a bad
    # LLM_FALLBACK_ORDER should be a boot error with a readable message, not a
    # 502 the first time someone opens a nibble. Makes no network or paid call.
    for warning in validate_llm_settings(settings):
        logger.warning("LLM config: %s", warning)
    create_tables()
    start_scheduler(_db_factory)
    yield
    # Shutdown
    stop_scheduler()


app = FastAPI(
    title="Nibbler API",
    description="Backend for Nibbler — AI-powered daily learning companion",
    version="1.0.0",
    lifespan=lifespan,
)

# ── Rate limiting (see app/rate_limit.py) ─────────────────────────────────────
app.state.limiter = limiter


def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    # slowapi's own default handler responds with {"error": "..."} — every
    # client-side error path here (api.js's shared `request()` AND the
    # hand-rolled uploadPdf) only ever reads `detail`, so a real rate limit
    # was silently swallowed into the generic "Upload failed"/"Request failed"
    # fallback text with zero indication of the real cause. Found 2026-07-25
    # after an upload failed with no useful reason attached.
    response = JSONResponse(
        {"detail": "You're doing that a bit too fast — wait a few minutes and try again."},
        status_code=429,
    )
    # Keep the Retry-After / X-RateLimit-* headers slowapi's own handler adds —
    # only the body shape changes here.
    return request.app.state.limiter._inject_headers(response, request.state.view_rate_limit)


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Only the website needs browser CORS; the native app sends no Origin header
# and is unaffected by this allowlist.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://getnibbler.com",
        "https://www.getnibbler.com",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(library.router)
app.include_router(bites.router)
app.include_router(streak.router)
app.include_router(notifications.router)
app.include_router(connect.router)
app.include_router(support.router)
app.include_router(revenuecat.router)
app.include_router(sync.router)


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env}


# ── Readiness check (Task 19, Aug 2026) ────────────────────────────────────────
# Distinct from /health on purpose: /health only ever proves this process is
# alive (always {"status": "ok"} once uvicorn is up, even mid-outage on the
# database). /ready additionally proves the database is reachable right now
# AND the schema this app relies on was verified present at boot — see
# `get_readiness_status()`/`verify_required_schema()` in app/database.py.
# NOT wired up as Railway's healthcheckPath by this change — see
# nibbler-backend/CLAUDE.md, "Deployment readiness (Task 19)" for the
# proposed (not applied) railway.toml edit and why it's left to Shafin.
@app.get("/ready")
async def ready():
    from app.database import get_readiness_status
    ok, detail = get_readiness_status()
    return JSONResponse(detail, status_code=200 if ok else 503)


@app.get("/")
async def root():
    return {"message": "🐱 Nibbler API is running"}
