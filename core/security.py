import base64
import binascii
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from core.config import settings


PASSWORD_ITERATIONS = 310_000


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt_value = salt or _b64encode(secrets.token_bytes(32))
    digest = hashlib.pbkdf2_hmac(
        "sha512",
        password.encode("utf-8"),
        _b64decode(salt_value),
        PASSWORD_ITERATIONS,
    )
    return _b64encode(digest), salt_value


def verify_password(password: str, password_hash: str, password_salt: str, iterations: int) -> bool:
    digest = hashlib.pbkdf2_hmac(
        "sha512",
        password.encode("utf-8"),
        _b64decode(password_salt),
        iterations,
    )
    return hmac.compare_digest(_b64encode(digest), password_hash)


def create_access_token(admin_id: int, username: str, token_version: int) -> str:
    issued_at = datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(minutes=settings.AUTH_TOKEN_EXPIRE_MINUTES)
    header = _b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    payload = _b64encode(json.dumps({
        "sub": str(admin_id),
        "username": username,
        "ver": token_version,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }, separators=(",", ":")).encode())
    signature = hmac.new(
        settings.AUTH_JWT_SECRET.encode("utf-8"),
        f"{header}.{payload}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{header}.{payload}.{_b64encode(signature)}"


def decode_access_token(token: str) -> dict:
    try:
        header, payload, signature = token.split(".")
        expected = hmac.new(
            settings.AUTH_JWT_SECRET.encode("utf-8"),
            f"{header}.{payload}".encode("ascii"),
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(_b64encode(expected), signature):
            raise ValueError("Invalid token signature")
        data = json.loads(_b64decode(payload))
        if data.get("exp", 0) <= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("Token has expired")
        if not data.get("sub"):
            raise ValueError("Token subject is missing")
        return data
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )


