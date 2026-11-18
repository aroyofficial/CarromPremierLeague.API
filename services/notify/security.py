"""OAuth state and refresh-token encryption.

The state token is an HMAC, not a bearer of the refresh token. Refresh tokens
are encrypted before they touch the database. A missing or invalid key refuses
to store anything rather than saving the token in the clear.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken


class StateError(Exception):
    pass


def sign_state(secret: str, provider: str, admin_id: int, now: datetime, ttl_seconds: int = 600) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    expires = int(now.timestamp()) + int(ttl_seconds)
    payload = f"{provider}:{int(admin_id)}:{expires}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    raw = f"{payload}:{digest}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def read_state(secret: str, token: str, now: datetime, expected_provider: str) -> int:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    padded = token + ("=" * (-len(token) % 4))
    try:
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        payload, digest = decoded.rsplit(":", 1)
        provider, admin_text, expires_text = payload.split(":")
    except (ValueError, UnicodeError) as exc:
        raise StateError("The calendar connection request is not valid.") from exc
    expected = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest):
        raise StateError("The calendar connection request is not valid.")
    if provider != expected_provider:
        raise StateError("The calendar connection request is not valid.")
    if int(expires_text) < int(now.timestamp()):
        raise StateError("The calendar connection request expired. Start it again.")
    return int(admin_text)


def pkce_verifier(secret: str, state: str) -> str:
    """RFC 7636 verifier bound to one state token. The callback derives it again, so nothing
    is stored, and a party holding only the code and the state cannot redeem the code."""
    digest = hmac.new(secret.encode("utf-8"), f"pkce:{state}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def encrypt_secret(key: str, value: str) -> str:
    try:
        return Fernet(key.encode("utf-8")).encrypt(value.encode("utf-8")).decode("ascii")
    except (ValueError, TypeError) as exc:
        raise StateError("CPL_CALENDAR_TOKEN_KEY is not a valid encryption key.") from exc


def decrypt_secret(key: str, value: str) -> str:
    try:
        return Fernet(key.encode("utf-8")).decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise StateError("The stored calendar credential could not be read.") from exc
