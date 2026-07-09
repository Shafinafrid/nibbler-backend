from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from app.database import create_tables, SessionLocal
from app.routers import auth, profile, library, bites, streak
from app.routers import notifications, connect
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

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # Tighten this in production
    allow_credentials=True,
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


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok", "env": settings.app_env}


@app.get("/")
async def root():
    return {"message": "🐱 Nibbler API is running"}
