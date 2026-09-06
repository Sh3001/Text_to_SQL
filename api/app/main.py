"""Query Warden API — FastAPI app. PipelineContext (schema snapshot +
value index) is built once at startup, not per request — introspection
and value-index construction are real DB round trips.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Before anything reads os.environ — DATABASE_URL and the settings
# below are resolved at import time.
from .config import load_env

load_env()

from .api.routes import router  # noqa: E402
from .history.routes import router as history_router  # noqa: E402
from .pipeline.generate import build_context  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden")

# Vite's dev server by default; override with a comma-separated list in
# any environment where the UI isn't served from localhost.
CORS_ORIGINS = [
    o.strip()
    for o in os.environ.get("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")
    if o.strip()
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    with psycopg.connect(DATABASE_URL) as conn:
        app.state.ctx = build_context(conn)
    # Pending plan_id -> PendingPlan, awaiting approve/reject — in-memory,
    # see api/routes.py's module docstring for why.
    app.state.pending_plans = {}
    yield


app = FastAPI(title="Query Warden", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(history_router)
app.include_router(router)
