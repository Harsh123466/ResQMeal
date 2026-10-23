"""AnnSetu API application entry point.

Route handlers live in app.api; database, schemas, dependencies, and reusable
workflow logic are kept in their own modules.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import database as _database
from .database import DATABASE_URL, MAX_COLLECTION_RADIUS_KM, ROOT, now, stamp
from .dependencies import admin_user, current_user, document_cipher, user_public
from .schemas import AuthLogin, AuthRegister, ConfirmIn, DonationIn, RecipientIn, ReviewIn
from .security import hash_password, issue_token, read_token, verify_password
from .api.auth import router as auth_router
from .api.donations import router as donations_router
from .api.health import router as health_router
from .api.impact import router as impact_router
from .api.notifications import router as notifications_router
from .api.portions import router as portions_router
from .api.recipients import router as recipients_router
from .services.common import (
    distance_km,
    donation_owned_by,
    donation_view,
    event,
    notification,
    portion_participant,
    recipient_owned_by,
    row_dict,
    safety_eligible,
)

# Kept as a small compatibility seam for tests and local tooling that override
# the database path through app.main.
DB_PATH: Path = _database.DB_PATH


def connect():
    _database.DB_PATH = DB_PATH
    return _database.connect()


def init_db() -> None:
    _database.DB_PATH = DB_PATH
    _database.init_db()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="AnnSetu AI",
    description="Surplus food rescue and recipient coordination",
    version="1.0.0",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("ANNSETU_FRONTEND_URL", "http://localhost:5173"), "http://127.0.0.1:5173"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

for api_router in (
    auth_router,
    notifications_router,
    health_router,
    recipients_router,
    donations_router,
    portions_router,
    impact_router,
):
    app.include_router(api_router)
