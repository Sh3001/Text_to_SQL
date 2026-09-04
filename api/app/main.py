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

from .api.routes import router
from .pipeline.generate import build_context

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/querywarden")


@asynccontextmanager
async def lifespan(app: FastAPI):
    with psycopg.connect(DATABASE_URL) as conn:
        app.state.ctx = build_context(conn)
    # Pending plan_id -> PlanResult, awaiting approve/reject — in-memory,
    # see api/routes.py's module docstring for why.
    app.state.pending_plans = {}
    yield


app = FastAPI(title="Query Warden", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Vite's dev server default — tightened to a real origin list before
    # this ever runs anywhere but localhost.
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
