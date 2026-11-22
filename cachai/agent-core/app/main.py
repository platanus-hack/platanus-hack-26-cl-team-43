from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.leaderboard import router as leaderboard_router
from app.api.routes import router
from app.api.telemetry import router as telemetry_router
from app.db import close_db_pool, init_db_pool
from app.middleware import RateLimitMiddleware
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db_pool()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        close_db_pool()


app = FastAPI(
    title="CachAI Agent Core",
    version="0.1.0",
    description="Autonomous multi-agent LLM security auditing & red-teaming backend.",
    lifespan=lifespan,
)

app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(telemetry_router)
app.include_router(leaderboard_router)
