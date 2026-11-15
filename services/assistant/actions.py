"""Signed pending actions. A write proposed by the model runs only after the same admin confirms it."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass

from core.config import settings

TTL_SECONDS = 600
_SECRET_FIELDS = {"password"}

_YES = re.compile(
    r"^\s*(y|yes|yeah|yep|yup|sure|ok|okay|confirm|confirmed|go|go ahead|do it|proceed|approve|approved|"
    r"please do|yes please|sounds good|ship it)\s*[.!]*\s*$",
    re.IGNORECASE,
)
_NO = re.compile(
    r"^\s*(n|no|nope|nah|cancel|stop|abort|don'?t|do not|never ?mind|forget it|no thanks)\s*[.!]*\s*$",
    re.IGNORECASE,
)


class ActionError(ValueError):
    pass


@dataclass(frozen=True)
class PendingAction:
    tool: str
    args: dict
    admin_id: int
    expires: int


def _key() -> bytes:
    return hashlib.sha256(("striker-action:" + settings.AUTH_JWT_SECRET).encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(tool: str, args: dict, admin_id: int, *, now: float | None = None) -> str:
    payload = {
        "t": tool,
        "a": args,
        "u": admin_id,
        "e": int((now or time.time()) + TTL_SECONDS),
        "n": secrets.token_hex(6),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), default=str).encode())
    mac = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def verify(token: str, admin_id: int, *, now: float | None = None) -> PendingAction:
    try:
        body, mac = token.split(".", 1)
        expected = _b64(hmac.new(_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(mac, expected):
            raise ActionError("This action could not be verified.")
        payload = json.loads(_unb64(body))
    except ActionError:
        raise
    except (ValueError, TypeError):
        raise ActionError("This action could not be verified.") from None
    if payload.get("u") != admin_id:
        raise ActionError("This action belongs to another admin.")
    if int(payload.get("e", 0)) < int(now or time.time()):
        raise ActionError("This action expired. Ask again and I'll prepare it fresh.")
    return PendingAction(tool=payload["t"], args=payload.get("a") or {}, admin_id=admin_id, expires=int(payload["e"]))


def masked(args: dict) -> dict:
    return {key: ("••••••" if key in _SECRET_FIELDS else value) for key, value in args.items()}


def is_yes(text: str) -> bool:
    return bool(_YES.match(text or ""))


def is_no(text: str) -> bool:
    return bool(_NO.match(text or ""))
