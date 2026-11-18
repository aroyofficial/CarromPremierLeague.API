"""HTTP surface for Web Push and calendar subscriptions."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.config import settings
from core.database import get_db
from core.response import ApiResponse
from repositories.notify_repository import NotifyRepository, public_subscription
from routes.auth_routes import get_current_admin, get_service
from schemas.notify_schema import (
    CalendarFeedRequest,
    PushPreferenceRequest,
    PushSubscribeRequest,
    PushUnsubscribeRequest,
)
from services.auth_service import AuthService
from services.match_rules import league_today
from services.notify.enqueue import backfill_connection
from services.notify.flags import NotifyFlags, load_flags
from services.notify.ics import calendar_etag, calendar_text, not_modified
from services.notify import telemetry
from services.notify.limits import RateLimiter
from services.notify.providers import (
    ProviderError,
    ProviderNotConfigured,
    exchange_google_code,
    exchange_microsoft_code,
    google_authorize_url,
    microsoft_authorize_url,
)
from services.notify.security import (
    StateError,
    decrypt_secret,
    encrypt_secret,
    pkce_challenge,
    pkce_verifier,
    read_state,
    sign_state,
)

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Notifications"])
_bearer = HTTPBearer(auto_error=False)
_limiter = RateLimiter()


def _flags() -> NotifyFlags:
    return load_flags()


def _client_key(request: Request) -> str:
    # The API runs behind the host's proxy, so the socket address is the proxy's. Same rule as the assistant limiter.
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _require_rate(request: Request) -> None:
    if not _limiter.allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="Too many requests. Wait a minute and try again.")


def optional_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    service: AuthService = Depends(get_service),
):
    if credentials is None or credentials.scheme.lower() != "bearer":
        return None
    try:
        return service.current_admin(credentials.credentials)
    except HTTPException:
        return None


def capabilities(flags: NotifyFlags | None = None) -> dict:
    flags = flags or load_flags()
    return {
        "push": {
            "enabled": flags.push_enabled,
            "ready": flags.push_ready(),
            "public_key": flags.vapid_public_key if flags.push_ready() else None,
            "reason": flags.push_block_reason(),
        },
        "calendar_feed": {
            "enabled": flags.calendar_feed_enabled,
            "ready": flags.feed_ready(),
            "reason": flags.feed_block_reason(),
        },
        "google": {
            "enabled": flags.google_enabled,
            "ready": flags.google_ready(),
            "reason": flags.google_block_reason(),
        },
        "microsoft": {
            "enabled": flags.microsoft_enabled,
            "ready": flags.microsoft_ready(),
            "reason": flags.microsoft_block_reason(),
        },
        "limits": {
            "match_time": "A match has a date and no start time. Calendar events are all-day, and the reminder is 09:00 IST on that date.",
            "venue": "Matches do not store a venue, so a venue change cannot be synced.",
            "postpone": "Moving the match date is the postpone. There is no separate postponed status.",
            "accounts": "Anyone can subscribe this device or a calendar feed. Connecting Google or Microsoft requires an admin sign-in.",
            "calendar_freshness": "The feed is current on every fetch. Calendar apps choose how often they fetch it.",
        },
    }


def _https_endpoint(value: str) -> str:
    endpoint = value.strip()
    if not endpoint.startswith("https://") or len(endpoint) > 2000:
        raise HTTPException(status_code=400, detail="The push endpoint must be an https URL.")
    return endpoint


def _preference_row(request: PushSubscribeRequest | PushPreferenceRequest, endpoint: str, admin_id: int | None, user_agent: str | None) -> dict:
    return {
        "endpoint": endpoint,
        "p256dh": getattr(request, "p256dh", ""),
        "auth": getattr(request, "auth", ""),
        "admin_id": admin_id,
        "season_id": request.season_id,
        "team_id": request.team_id,
        "user_agent": (user_agent or "")[:400] or None,
        "notify_scheduled": request.notify_scheduled,
        "notify_changed": request.notify_changed,
        "notify_cancelled": request.notify_cancelled,
        "notify_live": request.notify_live,
        "notify_ended": request.notify_ended,
        "notify_score": request.notify_score,
        "notify_reminder": request.notify_reminder,
    }


def feed_urls(base: str, token: str) -> dict:
    https = f"{base.rstrip('/')}/calendar/feeds/{token}.ics"
    webcal = https.replace("https://", "webcal://", 1).replace("http://", "webcal://", 1)
    return {"https_url": https, "webcal_url": webcal}


def _http_post(url, data=None, headers=None):
    with httpx.Client(timeout=20) as client:
        return client.post(url, data=data, headers=headers)


@router.get("/notifications/capabilities")
def get_capabilities():
    return ApiResponse(success=True, message="Notification capabilities", data=capabilities())


@router.post("/notifications/subscriptions")
def subscribe(
    body: PushSubscribeRequest,
    request: Request,
    db=Depends(get_db),
    admin=Depends(optional_admin),
):
    flags = _flags()
    if not flags.push_ready():
        raise HTTPException(status_code=404, detail=flags.push_block_reason() or "Push is off.")
    _require_rate(request)
    endpoint = _https_endpoint(body.endpoint)
    repository = NotifyRepository(db)
    if body.season_id is not None and not repository.season_exists(body.season_id):
        raise HTTPException(status_code=404, detail="Season not found.")
    if body.team_id is not None and not repository.team_exists(body.team_id):
        raise HTTPException(status_code=404, detail="Team not found.")
    row = _preference_row(body, endpoint, admin.id if admin else None, request.headers.get("user-agent"))
    previous = None
    if body.replaces_endpoint:
        previous = repository.subscription_by_endpoint(_https_endpoint(body.replaces_endpoint))
        if previous and not previous["void"]:
            row.update({key: previous[key] for key in ROTATED_FIELDS})
            row["admin_id"] = row["admin_id"] or previous.get("admin_id")
        else:
            previous = None
            # The service worker and the page can both report one rotation. The second report
            # must not reset the choices the first one carried over.
            current = repository.subscription_by_endpoint(endpoint)
            if current and not current["void"]:
                return ApiResponse(success=True, message="This device will get match alerts.", data=public_subscription(current))
    saved = repository.save_subscription(row)
    if previous and previous["endpoint"] != endpoint:
        repository.void_subscription(previous["endpoint"])
    return ApiResponse(success=True, message="This device will get match alerts.", data=public_subscription(saved))


ROTATED_FIELDS = (
    "season_id",
    "team_id",
    "notify_scheduled",
    "notify_changed",
    "notify_cancelled",
    "notify_live",
    "notify_ended",
    "notify_score",
    "notify_reminder",
)


@router.put("/notifications/subscriptions")
def update_subscription(body: PushPreferenceRequest, db=Depends(get_db)):
    if not _flags().push_ready():
        raise HTTPException(status_code=404, detail="Push is off.")
    endpoint = _https_endpoint(body.endpoint)
    saved = NotifyRepository(db).update_preferences(
        endpoint,
        {
            "season_id": body.season_id,
            "team_id": body.team_id,
            "notify_scheduled": body.notify_scheduled,
            "notify_changed": body.notify_changed,
            "notify_cancelled": body.notify_cancelled,
            "notify_live": body.notify_live,
            "notify_ended": body.notify_ended,
            "notify_score": body.notify_score,
            "notify_reminder": body.notify_reminder,
        },
    )
    if not saved:
        raise HTTPException(status_code=404, detail="This device is not subscribed.")
    return ApiResponse(success=True, message="Alert choices saved.", data=public_subscription(saved))


@router.delete("/notifications/subscriptions")
def unsubscribe(body: PushUnsubscribeRequest, db=Depends(get_db)):
    endpoint = _https_endpoint(body.endpoint)
    if not NotifyRepository(db).void_subscription(endpoint):
        raise HTTPException(status_code=404, detail="This device is not subscribed.")
    return ApiResponse(success=True, message="Alerts turned off for this device.", data=True)


@router.post("/calendar/feeds")
def create_feed(body: CalendarFeedRequest, request: Request, db=Depends(get_db)):
    flags = _flags()
    if not flags.feed_ready():
        raise HTTPException(status_code=404, detail=flags.feed_block_reason() or "Calendar feeds are off.")
    _require_rate(request)
    repository = NotifyRepository(db)
    if not repository.season_exists(body.season_id):
        raise HTTPException(status_code=404, detail="Season not found.")
    if body.team_id is not None and not repository.team_exists(body.team_id):
        raise HTTPException(status_code=404, detail="Team not found.")
    token = secrets.token_urlsafe(32)
    label = "Carrom Premier League"
    repository.create_feed(body.season_id, body.team_id, token, label)
    urls = feed_urls(str(request.base_url).rstrip("/"), token)
    return ApiResponse(
        success=True,
        message="Subscribe to this link. Calendar apps refresh it when matches change.",
        data={"token": token, "season_id": body.season_id, "team_id": body.team_id, **urls},
    )


@router.delete("/calendar/feeds/{token}")
def revoke_feed(token: str, db=Depends(get_db)):
    if not NotifyRepository(db).revoke_feed(token):
        raise HTTPException(status_code=404, detail="Calendar feed not found.")
    return ApiResponse(success=True, message="Calendar feed revoked.", data=True)


@router.get("/calendar/feeds/{token}.ics")
def season_feed(token: str, request: Request, db=Depends(get_db)):
    flags = _flags()
    if not flags.feed_ready():
        raise HTTPException(status_code=404, detail="Calendar feeds are off.")
    repository = NotifyRepository(db)
    feed = repository.feed_by_token(token)
    if not feed:
        raise HTTPException(status_code=404, detail="Calendar feed not found.")
    matches = repository.feed_matches(feed["season_id"], feed["team_id"])
    body = calendar_text(matches, feed["label"], flags.public_web_url)
    etag = calendar_etag(body)
    if not_modified(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": f'"{etag}"', "Cache-Control": "private, max-age=60, must-revalidate"})
    return Response(
        content=body,
        media_type="text/calendar",
        headers={
            "ETag": f'"{etag}"',
            "Cache-Control": "private, max-age=60, must-revalidate",
            "Content-Disposition": 'inline; filename="cpl.ics"',
        },
    )


@router.get("/calendar/matches/{match_id}.ics")
def match_download(match_id: int, db=Depends(get_db)):
    flags = _flags()
    if not flags.feed_ready():
        raise HTTPException(status_code=404, detail="Calendar feeds are off.")
    match = NotifyRepository(db).match_notice(match_id)
    if not match:
        raise HTTPException(status_code=404, detail="Match not found.")
    body = calendar_text([match], "Carrom Premier League", flags.public_web_url)
    return Response(
        content=body,
        media_type="text/calendar",
        headers={"Cache-Control": "no-store", "Content-Disposition": f'inline; filename="match-{match_id}.ics"'},
    )


def _connect(provider: str, admin, flags: NotifyFlags) -> dict:
    if admin is None:
        raise HTTPException(status_code=401, detail="Sign in as an admin to connect a calendar.")
    now = datetime.now(timezone.utc)
    state = sign_state(settings.AUTH_JWT_SECRET, provider, admin.id, now)
    challenge = pkce_challenge(pkce_verifier(settings.AUTH_JWT_SECRET, state))
    try:
        if provider == "google":
            url = google_authorize_url(flags, state, challenge)
        else:
            url = microsoft_authorize_url(flags, state, challenge)
    except ProviderNotConfigured as exc:
        raise HTTPException(status_code=503, detail=exc.reason) from exc
    return {"authorize_url": url}


@router.post("/calendar/google/connect")
def connect_google(admin=Depends(get_current_admin)):
    return ApiResponse(success=True, message="Continue in Google.", data=_connect("google", admin, _flags()))


@router.post("/calendar/microsoft/connect")
def connect_microsoft(admin=Depends(get_current_admin)):
    return ApiResponse(success=True, message="Continue in Microsoft.", data=_connect("microsoft", admin, _flags()))


@router.get("/notifications/telemetry")
def notify_telemetry(admin=Depends(get_current_admin), db=Depends(get_db)):
    """Process counters (reset on restart) plus backlog and failure counts read from the database."""
    try:
        health = NotifyRepository(db).delivery_health()
    except Exception:
        logger.info("match alert health counts unavailable")
        health = None
    return ApiResponse(
        success=True,
        message="Match alert counters",
        data={"process_counts": telemetry.snapshot(), "delivery_health": health},
    )


CONNECTION_STATES = {"active": "connected", "revoked": "reauthorization_required"}
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"


def sync_state(status: str, sync: dict | None) -> str:
    """What the admin sees for one connection: connected, update_pending, synced, sync_failed, or reauthorization_required."""
    if status == "revoked":
        return "reauthorization_required"
    sync = sync or {}
    if sync.get("last_status") == "failed":
        return "sync_failed"
    if sync.get("queue_pending"):
        return "update_pending"
    if sync.get("last_status") in ("sent", "skipped"):
        return "synced"
    return CONNECTION_STATES.get(status, status)


def _connection_public(row: dict, sync: dict | None = None) -> dict:
    sync = sync or {}
    last_at = sync.get("last_at")
    return {
        "id": row["id"],
        "provider": row["provider"],
        "state": CONNECTION_STATES.get(row["status"], row["status"]),
        "sync_state": sync_state(row["status"], sync),
        "last_synced_at": last_at.isoformat() if last_at else None,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


@router.get("/calendar/connections")
def list_connections(admin=Depends(get_current_admin), db=Depends(get_db)):
    repository = NotifyRepository(db)
    rows = repository.connections_for_admin(admin.id)
    try:
        states = repository.connection_sync_states([row["id"] for row in rows])
    except Exception:
        logger.info("calendar sync states unavailable")
        states = {}
    return ApiResponse(
        success=True,
        message="Calendar connections",
        data=[_connection_public(row, states.get(row["id"])) for row in rows],
    )


@router.delete("/calendar/connections/{connection_id}")
def disconnect(connection_id: int, admin=Depends(get_current_admin), db=Depends(get_db)):
    repository = NotifyRepository(db)
    row = repository.connection_for_admin(connection_id, admin.id)
    if not row:
        raise HTTPException(status_code=404, detail="Calendar connection not found.")
    flags = _flags()
    if row["provider"] == "google" and row["status"] == "active" and row["refresh_token"] and flags.token_key:
        # Best effort: the stored token is removed either way. Revoking it at Google stops the grant itself.
        try:
            refresh = decrypt_secret(flags.token_key, row["refresh_token"])
            _http_post(GOOGLE_REVOKE, data={"token": refresh}, headers={"Accept": "application/json"})
        except Exception:
            logger.info("google grant revoke did not complete connection=%s", connection_id)
    repository.disconnect_connection(connection_id, admin.id)
    return ApiResponse(
        success=True,
        message="Calendar disconnected. Events already on the calendar stay until you delete them.",
        data=True,
    )


def _finish_oauth(provider: str, code: str | None, state: str | None, db) -> RedirectResponse:
    flags = _flags()
    home = f"{flags.public_web_url}/schedule"
    if not code or not state:
        return RedirectResponse(f"{home}?calendar=denied")
    try:
        admin_id = read_state(settings.AUTH_JWT_SECRET, state, datetime.now(timezone.utc), provider)
        verifier = pkce_verifier(settings.AUTH_JWT_SECRET, state)
        if provider == "google":
            token = exchange_google_code(flags, code, _http_post, verifier)
            calendar_id = "primary"
        else:
            token = exchange_microsoft_code(flags, code, _http_post, verifier)
            calendar_id = "me"
        refresh = token.get("refresh_token")
        if not refresh:
            return RedirectResponse(f"{home}?calendar=missing-refresh")
        stored = encrypt_secret(flags.token_key, refresh)
        connection_id = NotifyRepository(db).save_connection(admin_id, provider, stored, calendar_id)
        backfill_connection(db, connection_id, league_today())
    except (ProviderNotConfigured, ProviderError, StateError):
        logger.info("calendar oauth failed provider=%s", provider)
        return RedirectResponse(f"{home}?calendar=error")
    return RedirectResponse(f"{home}?calendar={provider}")


@router.get("/calendar/google/callback")
def google_callback(code: str | None = None, state: str | None = None, db=Depends(get_db)):
    return _finish_oauth("google", code, state, db)


@router.get("/calendar/microsoft/callback")
def microsoft_callback(code: str | None = None, state: str | None = None, db=Depends(get_db)):
    return _finish_oauth("microsoft", code, state, db)
