from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _secret() -> bytes:
    configured = os.getenv("ANNSETU_SECRET_KEY")
    if configured:
        return configured.encode("utf-8")
    path = ROOT / ".annsetu_secret"
    if not path.exists():
        path.write_bytes(secrets.token_bytes(48))
    return path.read_bytes()


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_part, digest_part = stored.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_part)
        expected = base64.urlsafe_b64decode(digest_part)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def issue_token(subject: str, role: str, lifetime: int = 60 * 60 * 12, extra: dict | None = None) -> str:
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload_data = {"sub": subject, "role": role, "exp": int(time.time()) + lifetime, **(extra or {})}
    payload = _b64(json.dumps(payload_data, separators=(",", ":")).encode())
    signing = f"{header}.{payload}".encode("ascii")
    signature = _b64(hmac.new(_secret(), signing, hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def read_token(token: str) -> dict | None:
    try:
        header, payload, signature = token.split(".")
        signing = f"{header}.{payload}".encode("ascii")
        expected = _b64(hmac.new(_secret(), signing, hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        data = json.loads(decoded)
        if int(data["exp"]) <= int(time.time()):
            return None
        return data
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
