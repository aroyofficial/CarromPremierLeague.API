"""The credential a subscriber needs on every webhook delivery: Authorization (Bearer or Basic) or one custom header.

The value is sealed before it is stored and unsealed only while sending. Responses expose a masked hint, never the value.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass

from core.secret_box import seal, unseal

AUTH_NONE = "none"
AUTH_BEARER = "bearer"
AUTH_BASIC = "basic"
AUTH_HEADER = "header"
AUTH_TYPES = (AUTH_NONE, AUTH_BEARER, AUTH_BASIC, AUTH_HEADER)

MAX_SECRET_LENGTH = 500
MAX_USERNAME_LENGTH = 200
MAX_HEADER_NAME_LENGTH = 100

# RFC 7230 token characters.
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Headers the delivery sets itself, or that would break the request.
RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "expect",
        "user-agent",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "proxy-authenticate",
    }
)
RESERVED_PREFIXES = ("x-cpl-",)


class WebhookAuthError(ValueError):
    """The credential the admin sent is not usable. The message is safe to show."""


@dataclass(frozen=True)
class WebhookAuth:
    auth_type: str = AUTH_NONE
    header_name: str | None = None
    ciphertext: str | None = None
    hint: str | None = None


NO_AUTH = WebhookAuth()


def _single_line(value: str, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WebhookAuthError(f"{label} is required.")
    if len(value) > limit:
        raise WebhookAuthError(f"{label} must be {limit} characters or fewer.")
    if any(ch in value for ch in "\r\n\x00"):
        raise WebhookAuthError(f"{label} cannot contain line breaks.")
    return value


def validate_header_name(name: str | None) -> str:
    cleaned = _single_line(name or "", "Header name", MAX_HEADER_NAME_LENGTH).strip()
    if not _HEADER_NAME.match(cleaned):
        raise WebhookAuthError("Header name can only use letters, digits and these characters: ! # $ % & ' * + - . ^ _ ` | ~")
    lowered = cleaned.lower()
    if lowered in RESERVED_HEADERS or lowered.startswith(RESERVED_PREFIXES):
        raise WebhookAuthError(f"The header {cleaned} is set by CPL and cannot be used for authorization.")
    return cleaned


def _mask(value: str) -> str:
    tail = value[-4:] if len(value) > 8 else ""
    return f"****{tail}"


def build_auth(
    auth_type: str | None,
    *,
    token: str | None = None,
    username: str | None = None,
    password: str | None = None,
    header_name: str | None = None,
    header_value: str | None = None,
) -> WebhookAuth:
    kind = (auth_type or AUTH_NONE).strip().lower()
    if kind not in AUTH_TYPES:
        raise WebhookAuthError("Choose None, Bearer token, Basic, or Custom header for authorization.")
    if kind == AUTH_NONE:
        return NO_AUTH
    if kind == AUTH_BEARER:
        secret = _single_line(token or "", "Bearer token", MAX_SECRET_LENGTH).strip()
        return WebhookAuth(kind, None, seal(json.dumps({"token": secret})), _mask(secret))
    if kind == AUTH_BASIC:
        user = _single_line(username or "", "Username", MAX_USERNAME_LENGTH)
        if ":" in user:
            raise WebhookAuthError("Username cannot contain a colon.")
        pwd = _single_line(password or "", "Password", MAX_SECRET_LENGTH)
        return WebhookAuth(kind, None, seal(json.dumps({"username": user, "password": pwd})), f"{user[:18]}:****")
    name = validate_header_name(header_name)
    value = _single_line(header_value or "", "Header value", MAX_SECRET_LENGTH).strip()
    return WebhookAuth(kind, name, seal(json.dumps({"value": value})), _mask(value))


def auth_headers(auth_type: str | None, header_name: str | None, ciphertext: str | None) -> dict[str, str]:
    """The headers to add to a delivery. Raises WebhookAuthError when the stored value cannot be read."""
    kind = (auth_type or AUTH_NONE).strip().lower()
    if kind == AUTH_NONE:
        return {}
    try:
        secret = json.loads(unseal(ciphertext or ""))
        if kind == AUTH_BEARER:
            return {"Authorization": f"Bearer {secret['token']}"}
        if kind == AUTH_BASIC:
            pair = f"{secret['username']}:{secret['password']}".encode("utf-8")
            return {"Authorization": f"Basic {base64.b64encode(pair).decode('ascii')}"}
        if kind == AUTH_HEADER and header_name:
            return {header_name: str(secret["value"])}
    except Exception as exc:
        raise WebhookAuthError("Stored webhook credential could not be read.") from exc
    raise WebhookAuthError("Stored webhook credential could not be read.")
