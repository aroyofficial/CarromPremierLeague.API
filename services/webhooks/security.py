from __future__ import annotations

import hashlib
import hmac
import ipaddress
import secrets
import socket
import urllib.parse

from core.config import settings
from core.secret_box import seal, unseal

SECRET_PREFIX = "whsec_"


class WebhookTargetError(ValueError):
    pass


def generate_signing_secret() -> tuple[str, str, str]:
    raw = f"{SECRET_PREFIX}{secrets.token_urlsafe(32)}"
    hint = f"{raw[:10]}...{raw[-4:]}"
    return raw, seal(raw), hint


def unseal_signing_secret(ciphertext: str) -> str:
    return unseal(ciphertext)


def sign_webhook_payload(secret: str, timestamp: int, payload_json: str) -> str:
    signed = f"{int(timestamp)}.{payload_json}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_webhook_signature(secret: str, timestamp: int, payload_json: str, header_value: str) -> bool:
    expected = sign_webhook_payload(secret, timestamp, payload_json)
    return hmac.compare_digest(expected, (header_value or "").strip())


def _is_forbidden_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def validate_webhook_url(url: str, *, resolve_dns: bool = False, app_env: str | None = None) -> str:
    env = (app_env or settings.APP_ENV or "prod").lower()
    cleaned = (url or "").strip()
    if not cleaned or len(cleaned) > 2000:
        raise WebhookTargetError("Enter a valid webhook URL.")
    parsed = urllib.parse.urlsplit(cleaned)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise WebhookTargetError("Webhook URL must include a hostname.")
    if parsed.username or parsed.password:
        raise WebhookTargetError("Webhook URL must not embed credentials.")

    if env == "dev":
        if scheme not in {"https", "http"}:
            raise WebhookTargetError("Webhook URL must start with https:// (or http:// in local dev).")
        if scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
            raise WebhookTargetError("Plain http:// webhooks are only allowed for localhost in dev.")
        return cleaned

    if scheme != "https":
        raise WebhookTargetError("Webhook URL must use https://.")
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local") or host.endswith(".internal"):
        raise WebhookTargetError("Webhook URL cannot target local or internal hosts.")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and _is_forbidden_ip(literal_ip):
        raise WebhookTargetError("Webhook URL cannot target a private or reserved IP address.")

    if resolve_dns and literal_ip is None:
        port = parsed.port or 443
        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise WebhookTargetError("Webhook hostname could not be resolved.") from exc
        if not infos:
            raise WebhookTargetError("Webhook hostname could not be resolved.")
        for info in infos:
            ip_text = info[4][0]
            try:
                resolved_ip = ipaddress.ip_address(ip_text)
            except ValueError:
                continue
            if _is_forbidden_ip(resolved_ip):
                raise WebhookTargetError("Webhook URL resolves to a private or reserved IP address.")

    return cleaned
