import asyncio
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials

from core.config import settings
from core.database import Database
from core.realtime import (
    PUBLIC_CHANNEL,
    HubFull,
    Ticket,
    admin_channel,
    format_sse,
    hub,
    origin_allowed,
    realtime_enabled,
    tickets,
)
from core.security import decode_access_token
from repositories.admin_repository import AdminRepository
from routes.auth_routes import _bearer_scheme, get_current_admin
from schemas.auth_schema import AdminResponse

logger = logging.getLogger("cpl.realtime")
router = APIRouter(prefix="/realtime", tags=["Realtime"])
KEEPALIVE_SECONDS = 15
REVOKE_EVERY_SECONDS = 60
# A named event, not a comment: the browser EventSource API hides comment lines, so a comment cannot prove the socket is alive.
HEARTBEAT = "event: realtime.heartbeat\ndata: {}\n\n"


def revocation_due(checked_at: float | None, now: float) -> bool:
    return checked_at is None or now - checked_at >= REVOKE_EVERY_SECONDS


def _require_on(request: Request) -> None:
    if not realtime_enabled():
        raise HTTPException(status_code=404, detail="Realtime updates are off.")
    if not origin_allowed(request.headers.get("origin"), settings.APP_ALLOWED_HOSTS):
        raise HTTPException(status_code=403, detail="This origin cannot subscribe.")


def _admin_still_valid(ticket: Ticket) -> bool:
    connection = Database.get_connection()
    try:
        record = AdminRepository(connection).get_credentials_by_username(ticket.username)
    finally:
        connection.close()
    if not record or int(record["Id"]) != ticket.admin_id:
        return False
    if int(record["TokenVersion"]) != ticket.token_version:
        return False
    return bool(record["IsActive"]) or bool(record.get("MustChangePassword"))


def _stream(request: Request, channel: str, queue, check=None, ends_at: float | None = None):
    async def events():
        checked_at = None
        try:
            yield "retry: 5000\n: connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if ends_at is not None and time.time() >= ends_at:
                    break
                try:
                    body = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if check is not None and revocation_due(checked_at, now):
                        checked_at = now
                        if not await check():
                            yield "event: realtime.revoked\ndata: {}\n\n"
                            break
                    yield HEARTBEAT
                    continue
                yield format_sse(body)
        finally:
            hub.unsubscribe(channel, queue)
            logger.info("subscriber left channel=%s subscribers=%s", channel.split(":", 1)[0], hub.subscriber_count())

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _subscribe(channel: str):
    try:
        queue = await hub.subscribe(channel)
    except HubFull:
        raise HTTPException(status_code=503, detail="Too many realtime subscribers.") from None
    logger.info("subscriber joined channel=%s subscribers=%s", channel.split(":", 1)[0], hub.subscriber_count())
    return queue


@router.get("/health", summary="Realtime hub status")
def realtime_health():
    return {
        "enabled": realtime_enabled(),
        "backplane": "memory (single process)",
        "subscribers": hub.subscriber_count(),
        "published": hub.published,
        "dropped": hub.dropped,
        "rejected": hub.rejected,
    }


@router.get("/public", summary="Public change notices")
async def public_stream(request: Request):
    _require_on(request)
    queue = await _subscribe(PUBLIC_CHANNEL)
    return _stream(request, PUBLIC_CHANNEL, queue)


@router.post("/ticket", summary="One-time ticket for this admin's private stream")
def admin_ticket(
    request: Request,
    admin: AdminResponse = Depends(get_current_admin),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
):
    _require_on(request)
    payload = decode_access_token(credentials.credentials)
    try:
        value = tickets.issue(Ticket(
            admin_id=admin.id,
            username=admin.username,
            token_version=int(payload["ver"]),
            token_expires_at=int(payload["exp"]),
        ))
    except HubFull:
        raise HTTPException(status_code=503, detail="Too many realtime subscribers.") from None
    return {"ticket": value, "expires_in": tickets.ttl_seconds}


@router.get("/admin", summary="This admin's private change notices")
async def admin_stream(request: Request, ticket: str = ""):
    _require_on(request)
    granted = tickets.consume(ticket)
    if granted is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Ask for a new realtime ticket.")
    channel = admin_channel(granted.admin_id)
    queue = await _subscribe(channel)

    async def check() -> bool:
        try:
            return await run_in_threadpool(_admin_still_valid, granted)
        except Exception:
            logger.exception("realtime admin check failed")
            return False

    return _stream(request, channel, queue, check=check, ends_at=float(granted.token_expires_at))
