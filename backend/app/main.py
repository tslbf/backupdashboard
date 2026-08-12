from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import router
from .collectors import close_orphaned_runs
from .config import get_settings
from .db import init_db
from .logbuffer import install as install_log_buffer
from .scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Keeps a bounded tail of the log in memory for the Collectors page.
install_log_buffer()

# httpx emits one INFO line per request. Azure's job pagination makes thousands
# of them, which flushes everything useful out of the 600-line buffer and buries
# the collectors' own progress. Warnings and errors still come through; set
# HTTP_LOG_LEVEL=INFO to get the per-request lines back while debugging.
logging.getLogger("httpx").setLevel(os.getenv("HTTP_LOG_LEVEL", "WARNING").upper())

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    close_orphaned_runs()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Backup Status Dashboard", lifespan=lifespan)
app.include_router(router)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# In production the built frontend is served by the same process — one Windows
# service, no separate web server needed. Unknown paths fall back to index.html
# so the SPA's client-side routes work on refresh/deep-link.
if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
