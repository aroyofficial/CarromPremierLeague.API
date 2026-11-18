"""Google Calendar and Microsoft Graph event bodies, plus OAuth calls.

Nothing here runs during a match request. The worker calls the upsert helpers.
When the server has no client credentials, the connect helpers refuse before
any network call.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

from services.notify.copy import description, summary
from services.notify.flags import NotifyFlags

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTS = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/calendar.events"
MICROSOFT_AUTH = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
MICROSOFT_TOKEN = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
MICROSOFT_EVENTS = "https://graph.microsoft.com/v1.0/me/events"
MICROSOFT_SCOPE = "offline_access Calendars.ReadWrite"


class ProviderNotConfigured(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ProviderError(Exception):
    """A provider call failed. Only `category` is ever stored or logged; the provider body is not kept."""

    category = "provider_error"
    retryable = True

    def __init__(self, status: int | None = None, retry_after: int | None = None):
        super().__init__(self.category)
        self.status = status
        self.retry_after = retry_after


class ProviderUnavailable(ProviderError):
    category = "provider_unavailable"
    retryable = True


class ProviderRateLimited(ProviderError):
    category = "rate_limited"
    retryable = True


class ProviderRejected(ProviderError):
    """A request the provider will not accept on a retry, such as a missing event."""

    category = "provider_rejected"
    retryable = False


class ReauthorizationRequired(ProviderError):
    """The stored grant was revoked or expired. Only the admin can fix this by connecting again."""

    category = "reauthorization_required"
    retryable = False


_GRANT_ERRORS = {"invalid_grant", "unauthorized_client", "invalid_client"}


def _retry_after(response) -> int | None:
    raw = str(getattr(response, "headers", {}).get("Retry-After", "") or "").strip()
    return int(raw) if raw.isdigit() else None


def provider_failure(response, status: int, *, token_request: bool = False) -> ProviderError:
    """Classify a failed provider response. The body is read for the grant error code only."""
    if token_request:
        try:
            body = response.json()
        except Exception:
            body = {}
        if isinstance(body, dict) and str(body.get("error") or "") in _GRANT_ERRORS:
            return ReauthorizationRequired(status)
    if status == 429:
        return ProviderRateLimited(status, _retry_after(response))
    if status >= 500 or status == 401:
        # A 401 on an event write means the access token expired mid-flight; the next attempt refreshes it.
        return ProviderUnavailable(status, _retry_after(response))
    return ProviderRejected(status)


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def google_event_id(match_id: int) -> str:
    return f"cplmatch{int(match_id):010d}"


def google_event_body(match: dict, public_web_url: str) -> dict:
    day = _day(match["scheduled_date"])
    end = day + timedelta(days=1)
    return {
        "id": google_event_id(int(match["id"])),
        "summary": summary(match),
        "description": description(match, public_web_url),
        "start": {"date": day.isoformat()},
        "end": {"date": end.isoformat()},
        "status": "cancelled" if match.get("void") else "confirmed",
        "transparency": "transparent",
        "source": {"title": "Carrom Premier League", "url": f"{public_web_url}/schedule"},
    }


def microsoft_event_body(match: dict, public_web_url: str) -> dict:
    day = _day(match["scheduled_date"])
    end = day + timedelta(days=1)
    return {
        "subject": summary(match),
        "body": {"contentType": "text", "content": description(match, public_web_url)},
        "isAllDay": True,
        "showAs": "free",
        "start": {"dateTime": f"{day.isoformat()}T00:00:00", "timeZone": "Asia/Kolkata"},
        "end": {"dateTime": f"{end.isoformat()}T00:00:00", "timeZone": "Asia/Kolkata"},
        "transactionId": google_event_id(int(match["id"])),
    }


def content_hash(body: dict) -> str:
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pkce(challenge: str | None) -> dict:
    return {"code_challenge": challenge, "code_challenge_method": "S256"} if challenge else {}


def google_authorize_url(flags: NotifyFlags, state: str, challenge: str | None = None) -> str:
    reason = flags.google_block_reason()
    if reason:
        raise ProviderNotConfigured(reason)
    query = urlencode(
        {
            "client_id": flags.google_client_id,
            "redirect_uri": f"{flags.oauth_redirect_base}/calendar/google/callback",
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
            **_pkce(challenge),
        }
    )
    return f"{GOOGLE_AUTH}?{query}"


def microsoft_authorize_url(flags: NotifyFlags, state: str, challenge: str | None = None) -> str:
    reason = flags.microsoft_block_reason()
    if reason:
        raise ProviderNotConfigured(reason)
    query = urlencode(
        {
            "client_id": flags.microsoft_client_id,
            "redirect_uri": f"{flags.oauth_redirect_base}/calendar/microsoft/callback",
            "response_type": "code",
            "scope": MICROSOFT_SCOPE,
            "prompt": "consent",
            "state": state,
            **_pkce(challenge),
        }
    )
    return f"{MICROSOFT_AUTH}?{query}"


def _form(post, url: str, data: dict) -> dict:
    response = post(url, data=data, headers={"Accept": "application/json"})
    status = getattr(response, "status_code", 0)
    if status >= 400:
        raise provider_failure(response, status, token_request=True)
    body = response.json()
    if not isinstance(body, dict):
        raise ProviderUnavailable(status)
    return body


def exchange_google_code(flags: NotifyFlags, code: str, post, verifier: str | None = None) -> dict:
    reason = flags.google_block_reason()
    if reason:
        raise ProviderNotConfigured(reason)
    return _form(
        post,
        GOOGLE_TOKEN,
        {
            "code": code,
            "client_id": flags.google_client_id,
            "client_secret": flags.google_client_secret,
            "redirect_uri": f"{flags.oauth_redirect_base}/calendar/google/callback",
            "grant_type": "authorization_code",
            **({"code_verifier": verifier} if verifier else {}),
        },
    )


def exchange_microsoft_code(flags: NotifyFlags, code: str, post, verifier: str | None = None) -> dict:
    reason = flags.microsoft_block_reason()
    if reason:
        raise ProviderNotConfigured(reason)
    return _form(
        post,
        MICROSOFT_TOKEN,
        {
            "code": code,
            "client_id": flags.microsoft_client_id,
            "client_secret": flags.microsoft_client_secret,
            "redirect_uri": f"{flags.oauth_redirect_base}/calendar/microsoft/callback",
            "grant_type": "authorization_code",
            **({"code_verifier": verifier} if verifier else {}),
        },
    )


def refresh_access_token(provider: str, flags: NotifyFlags, refresh_token: str, post) -> str:
    if provider == "google":
        body = _form(
            post,
            GOOGLE_TOKEN,
            {
                "client_id": flags.google_client_id,
                "client_secret": flags.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    elif provider == "microsoft":
        body = _form(
            post,
            MICROSOFT_TOKEN,
            {
                "client_id": flags.microsoft_client_id,
                "client_secret": flags.microsoft_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": MICROSOFT_SCOPE,
            },
        )
    else:
        raise ProviderNotConfigured("Unknown calendar provider.")
    token = body.get("access_token")
    if not token:
        raise ProviderNotConfigured("The calendar provider did not return an access token.")
    return str(token)


def _status(response) -> int:
    return int(getattr(response, "status_code", 500) or 500)


def upsert_google_event(access_token: str, match: dict, public_web_url: str, request) -> str:
    """Update the match's event, creating it under the same stable id the first time.

    Google's update call returns 404 for an id that does not exist yet, and its insert
    call returns 409 for an id that does. Either order lands on exactly one event.
    """
    body = google_event_body(match, public_web_url)
    event_id = body["id"]
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    response = request("PUT", f"{GOOGLE_EVENTS}/{event_id}", json=body, headers=headers)
    status = _status(response)
    if status == 404:
        response = request("POST", GOOGLE_EVENTS, json=body, headers=headers)
        status = _status(response)
        if status == 409:
            response = request("PUT", f"{GOOGLE_EVENTS}/{event_id}", json=body, headers=headers)
            status = _status(response)
    if status >= 400:
        raise provider_failure(response, status)
    return event_id


def upsert_microsoft_event(access_token: str, match: dict, public_web_url: str, existing_id: str | None, request) -> str:
    """Write the match's event and return its Graph id. A voided match deletes the event.

    Graph has no client-chosen ids, so only a 404 on the stored id (the user deleted the
    event) creates a new one. Any other failure is raised, never turned into a second event.
    `transactionId` makes a retried create return the first event instead of a duplicate.
    """
    body = microsoft_event_body(match, public_web_url)
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
    if match.get("void"):
        if not existing_id:
            return ""
        response = request("DELETE", f"{MICROSOFT_EVENTS}/{existing_id}", json=None, headers=headers)
        status = _status(response)
        if status >= 400 and status != 404:
            raise provider_failure(response, status)
        return existing_id
    if existing_id:
        response = request("PATCH", f"{MICROSOFT_EVENTS}/{existing_id}", json=body, headers=headers)
        status = _status(response)
        if status < 400:
            return existing_id
        if status != 404:
            raise provider_failure(response, status)
    response = request("POST", MICROSOFT_EVENTS, json=body, headers=headers)
    status = _status(response)
    if status >= 400:
        raise provider_failure(response, status)
    created = response.json() if hasattr(response, "json") else {}
    created_id = str((created or {}).get("id") or "")
    if not created_id:
        raise ProviderUnavailable(status)
    return created_id
