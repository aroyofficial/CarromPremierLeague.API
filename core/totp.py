"""Time-based one-time passwords (RFC 6238) for authenticator apps such as Microsoft Authenticator."""

import base64
import hmac
import secrets
import struct
import time
from hashlib import sha1
from urllib.parse import quote, urlencode

PERIOD = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    cleaned = secret.replace(" ", "").upper()
    return base64.b32decode(cleaned + "=" * (-len(cleaned) % 8))


def step_at(moment: float | None = None) -> int:
    return int((time.time() if moment is None else moment) // PERIOD)


def code_for(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 10 ** DIGITS:0{DIGITS}d}"


def match(secret: str, code: str, moment: float | None = None, last_step: int | None = None, window: int = 1) -> int | None:
    """Return the matching time step, or None. A step at or before last_step is a replay and never matches."""
    code = (code or "").replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    current = step_at(moment)
    for step in range(current - window, current + window + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(code_for(secret, step), code):
            return step
    return None


def provisioning_uri(secret: str, account: str, issuer: str) -> str:
    label = quote(f"{issuer}:{account}")
    query = urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1", "digits": DIGITS, "period": PERIOD})
    return f"otpauth://totp/{label}?{query}"
