"""Feature flags for match alerts and calendars.

Every switch defaults off. A missing secret keeps that channel off even when its
switch is on, so a deploy cannot start sending with an empty configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _on(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _present(value: str | None) -> str:
    return (value or "").strip()


@dataclass(frozen=True)
class NotifyFlags:
    push_enabled: bool
    calendar_feed_enabled: bool
    google_enabled: bool
    microsoft_enabled: bool
    vapid_public_key: str
    vapid_private_key: str
    vapid_subject: str
    google_client_id: str
    google_client_secret: str
    microsoft_client_id: str
    microsoft_client_secret: str
    oauth_redirect_base: str
    token_key: str
    public_web_url: str
    worker_enabled: bool
    # Staged rollout: "match started" pushes, and live or result changes written to
    # connected calendars, each have their own switch on top of the channel switch.
    push_live_enabled: bool = False
    calendar_live_updates_enabled: bool = False

    def push_block_reason(self) -> str | None:
        if not self.push_enabled:
            return "CPL_PUSH_ENABLED is off."
        if not self.vapid_public_key or not self.vapid_private_key:
            return "VAPID keys are not configured."
        if not self.vapid_subject:
            return "CPL_VAPID_SUBJECT is not configured."
        return None

    def push_ready(self) -> bool:
        return self.push_block_reason() is None

    def feed_block_reason(self) -> str | None:
        if not self.calendar_feed_enabled:
            return "CPL_CALENDAR_FEED_ENABLED is off."
        return None

    def feed_ready(self) -> bool:
        return self.feed_block_reason() is None

    def google_block_reason(self) -> str | None:
        if not self.google_enabled:
            return "CPL_GOOGLE_CALENDAR_ENABLED is off."
        missing = [
            name
            for name, value in (
                ("CPL_GOOGLE_CALENDAR_CLIENT_ID", self.google_client_id),
                ("CPL_GOOGLE_CALENDAR_CLIENT_SECRET", self.google_client_secret),
                ("CPL_OAUTH_REDIRECT_BASE", self.oauth_redirect_base),
                ("CPL_CALENDAR_TOKEN_KEY", self.token_key),
            )
            if not value
        ]
        if missing:
            return "Missing " + ", ".join(missing) + "."
        return None

    def google_ready(self) -> bool:
        return self.google_block_reason() is None

    def microsoft_block_reason(self) -> str | None:
        if not self.microsoft_enabled:
            return "CPL_MICROSOFT_CALENDAR_ENABLED is off."
        missing = [
            name
            for name, value in (
                ("CPL_MICROSOFT_CALENDAR_CLIENT_ID", self.microsoft_client_id),
                ("CPL_MICROSOFT_CALENDAR_CLIENT_SECRET", self.microsoft_client_secret),
                ("CPL_OAUTH_REDIRECT_BASE", self.oauth_redirect_base),
                ("CPL_CALENDAR_TOKEN_KEY", self.token_key),
            )
            if not value
        ]
        if missing:
            return "Missing " + ", ".join(missing) + "."
        return None

    def microsoft_ready(self) -> bool:
        return self.microsoft_block_reason() is None

    def delivery_ready(self) -> bool:
        return self.push_ready() or self.google_ready() or self.microsoft_ready()


def load_flags(env: dict | None = None) -> NotifyFlags:
    source = os.environ if env is None else env
    public_web = _present(source.get("CPL_PUBLIC_WEB_URL")) or "https://aroyofficial.github.io/CarromPremierLeague.Web"
    return NotifyFlags(
        push_enabled=_on(source.get("CPL_PUSH_ENABLED")),
        calendar_feed_enabled=_on(source.get("CPL_CALENDAR_FEED_ENABLED")),
        google_enabled=_on(source.get("CPL_GOOGLE_CALENDAR_ENABLED")),
        microsoft_enabled=_on(source.get("CPL_MICROSOFT_CALENDAR_ENABLED")),
        vapid_public_key=_present(source.get("CPL_VAPID_PUBLIC_KEY")),
        vapid_private_key=_present(source.get("CPL_VAPID_PRIVATE_KEY")),
        vapid_subject=_present(source.get("CPL_VAPID_SUBJECT")),
        google_client_id=_present(source.get("CPL_GOOGLE_CALENDAR_CLIENT_ID")),
        google_client_secret=_present(source.get("CPL_GOOGLE_CALENDAR_CLIENT_SECRET")),
        microsoft_client_id=_present(source.get("CPL_MICROSOFT_CALENDAR_CLIENT_ID")),
        microsoft_client_secret=_present(source.get("CPL_MICROSOFT_CALENDAR_CLIENT_SECRET")),
        oauth_redirect_base=_present(source.get("CPL_OAUTH_REDIRECT_BASE")).rstrip("/"),
        token_key=_present(source.get("CPL_CALENDAR_TOKEN_KEY")),
        public_web_url=public_web.rstrip("/"),
        worker_enabled=_on(source.get("CPL_NOTIFY_WORKER") or "on") or _present(source.get("CPL_NOTIFY_WORKER")) == "",
        push_live_enabled=_on(source.get("CPL_PUSH_LIVE_ENABLED")),
        calendar_live_updates_enabled=_on(source.get("CPL_CALENDAR_LIVE_UPDATES_ENABLED")),
    )
