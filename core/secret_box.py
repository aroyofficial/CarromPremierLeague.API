"""Encrypts small secrets, such as TOTP seeds, before they are stored. The key is derived from the JWT secret."""

import base64
import os
from hashlib import sha256

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.config import settings


def _cipher() -> AESGCM:
    return AESGCM(sha256(f"cpl-secret-box:{settings.AUTH_JWT_SECRET}".encode()).digest())


def seal(value: str) -> str:
    nonce = os.urandom(12)
    sealed = _cipher().encrypt(nonce, value.encode(), b"cpl")
    return base64.urlsafe_b64encode(nonce + sealed).decode("ascii")


def unseal(token: str) -> str:
    raw = base64.urlsafe_b64decode(token.encode("ascii"))
    return _cipher().decrypt(raw[:12], raw[12:], b"cpl").decode()
