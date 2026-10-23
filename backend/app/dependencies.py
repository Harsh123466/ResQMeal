from __future__ import annotations

import os
import sqlite3
from uuid import uuid4

from cryptography.fernet import Fernet
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .database import ROOT, connect, stamp
from .security import read_token

bearer = HTTPBearer(auto_error=False)

def user_public(user: sqlite3.Row | dict) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)) -> dict:
    if credentials is None:
        raise HTTPException(401, "Sign in to continue", headers={"WWW-Authenticate": "Bearer"})
    claims = read_token(credentials.credentials)
    if not claims:
        raise HTTPException(401, "Session expired; sign in again", headers={"WWW-Authenticate": "Bearer"})
    with connect() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (claims.get("sub"),)).fetchone()
        if not row or row["role"] != claims.get("role"):
            raise HTTPException(401, "Account not found")
        if row["role"] not in ("donor", "recipient", "admin"):
            raise HTTPException(403, "This account role is no longer supported")
        return user_public(row)


def admin_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Administrator access is required")
    return user


def document_cipher() -> Fernet:
    configured = os.getenv("ANNSETU_DOCUMENT_KEY", "").strip()
    path = ROOT / ".annsetu_docs.key"
    if configured:
        key = configured.encode("ascii")
    else:
        if not path.exists():
            path.write_bytes(Fernet.generate_key())
        key = path.read_bytes()
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise HTTPException(500, "ANNSETU_DOCUMENT_KEY must be a valid Fernet key") from exc
