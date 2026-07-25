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
from app.config import get_settings

settings = get_settings()


def _db_factory():
    return SessionLocal()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
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


@app.get("/")
async def root():
    return {"message": "🐱 Nibbler API is running"}
