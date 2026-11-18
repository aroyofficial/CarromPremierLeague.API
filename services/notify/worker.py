"""Drain the outbox on the API process.

The league runs as one web process and has no separate queue. Pending rows
survive a restart. A second process could deliver a row twice; the calendar
write is hashed, and a push uses a stable tag so the replacement is the same
alert.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date

from core.database import Database
from repositories.notify_repository import NotifyRepository
from services.match_rules import league_today
from services.notify.delivery import delivery_plan
from services.notify.events import CANCELLED, SCHEDULE
from services.notify.flags import NotifyFlags, load_flags
from services.notify.providers import (
    ProviderError,
    ReauthorizationRequired,
    content_hash,
    google_event_body,
    microsoft_event_body,
    refresh_access_token,
    upsert_google_event,
    upsert_microsoft_event,
)
from services.notify import telemetry
from services.notify.push import push_payload, send_web_push
from services.notify.security import decrypt_secret

logger = logging.getLogger(__name__)

_last_reminder_day: date | None = None


def _post(url, data=None, headers=None):
    import httpx

    with httpx.Client(timeout=20) as client:
        return client.post(url, data=data, headers=headers)


def _request(method, url, json=None, headers=None):
    import httpx

    with httpx.Client(timeout=20) as client:
        return client.request(method, url, json=json, headers=headers)


def enqueue_reminders(repository: NotifyRepository, day: date) -> None:
    global _last_reminder_day
    if _last_reminder_day == day:
        return
    for match in repository.due_reminders(day):
        repository.enqueue(match, "reminder", key=f"{match['id']}:reminder:{day.isoformat()}")
    _last_reminder_day = day


def deliver_item(repository: NotifyRepository, item: dict, flags: NotifyFlags, post, request) -> None:
    payload = json.loads(item["Payload"])
    event = payload.get("event") or item["EventType"]
    snapshot = payload.get("match") or {}
    before = payload.get("before")
    # A missing row was deleted (a schedule rebuild deletes unplayed matches), so any notice
    # for it, including a late retry of an older one, may only withdraw the calendar event.
    current = repository.match_notice(int(item["MatchId"]))
    live = current or dict(snapshot, void=True)
    pushable = current is not None or event in (CANCELLED, SCHEDULE)
    subscriptions = repository.active_subscriptions() if flags.push_ready() and pushable else []
    connections = repository.active_connections() if flags.google_ready() or flags.microsoft_ready() else []
    actions = delivery_plan(event, live, subscriptions, connections, flags, payload.get("connection_id"))
    # A retry must not alert devices that already got this notice on an earlier attempt.
    already_pushed = repository.sent_targets(int(item["Id"]), "push") if int(item.get("Attempts") or 1) > 1 else set()
    failed = False
    retry_reason = ""
    retry_after: int | None = None
    for action in actions:
        if action["channel"] == "push" and str(action.get("target")) in already_pushed:
            continue
        if action["status"] == "skipped":
            repository.record_delivery(int(item["Id"]), action["channel"], "", "skipped", action.get("reason") or "")
            telemetry.count("delivery.skipped", action["channel"], action.get("reason") or "policy")
            continue
        channel = action["channel"]
        try:
            if channel == "push":
                _deliver_push(repository, item, snapshot, before, action["subscription"], flags)
            elif channel == "google":
                _deliver_google(repository, item, live, action["connection"], flags, post, request)
            elif channel == "microsoft":
                _deliver_microsoft(repository, item, live, action["connection"], flags, post, request)
        except ProviderError as exc:
            # A revoked grant stops that connection. Only the admin can fix it, so nothing retries.
            if isinstance(exc, ReauthorizationRequired):
                repository.set_connection_status(int(action["connection"]["id"]), "revoked")
                telemetry.count("connection.revoked", channel)
                logger.info("calendar grant needs the admin to connect again channel=%s", channel)
            if exc.retryable:
                failed = True
                retry_reason = retry_reason or exc.category
                retry_after = max(retry_after or 0, exc.retry_after or 0) or None
            telemetry.count("delivery.failed", channel, exc.category)
            repository.record_delivery(int(item["Id"]), channel, str(action.get("target") or ""), "failed", exc.category)
        except Exception as exc:
            failed = True
            # Only the exception type is kept: messages can echo provider bodies or endpoints.
            detail = f"unexpected: {type(exc).__name__}"
            retry_reason = retry_reason or "delivery failed"
            logger.info("notify delivery failed channel=%s outbox=%s error=%s", channel, item["Id"], type(exc).__name__)
            telemetry.count("delivery.failed", channel, "unexpected")
            repository.record_delivery(int(item["Id"]), channel, str(action.get("target") or ""), "failed", detail)
    if failed:
        telemetry.count("outbox.retry")
        repository.mark_retry(int(item["Id"]), int(item["Attempts"]), retry_reason or "delivery failed", retry_after)
    else:
        telemetry.count("outbox.done")
        repository.mark_done(int(item["Id"]))


def _deliver_push(repository, item, snapshot, before, subscription, flags: NotifyFlags) -> None:
    body = push_payload(item["EventType"], snapshot, before, flags.public_web_url, int(item["Id"]))
    result = send_web_push(
        subscription,
        body,
        private_key=flags.vapid_private_key,
        subject=flags.vapid_subject,
    )
    if result["ok"]:
        telemetry.count("delivery.sent", "push")
        repository.record_delivery(int(item["Id"]), "push", str(subscription["id"]), "sent", "")
        return
    if result["gone"] or result["blocked"]:
        if result["gone"]:
            telemetry.count("push.subscription_gone")
            repository.note_push_failure(int(subscription["id"]), gone=True)
        repository.record_delivery(int(item["Id"]), "push", str(subscription["id"]), "skipped", result["reason"])
        return
    repository.note_push_failure(int(subscription["id"]), gone=False)
    raise RuntimeError(result["reason"])


def _deliver_google(repository, item, match, connection, flags, post, request) -> None:
    body = google_event_body(match, flags.public_web_url)
    digest = content_hash(body)
    current = repository.event_map(int(connection["id"]), int(match["id"]))
    if current and current["content_hash"] == digest:
        telemetry.count("delivery.skipped", "google", "unchanged")
        repository.record_delivery(int(item["Id"]), "google", str(connection["id"]), "skipped", "unchanged")
        return
    refresh = decrypt_secret(flags.token_key, connection["refresh_token"])
    access = refresh_access_token("google", flags, refresh, post)
    external_id = upsert_google_event(access, match, flags.public_web_url, request)
    repository.save_event_map(int(connection["id"]), int(match["id"]), external_id, digest)
    telemetry.count("delivery.sent", "google")
    repository.record_delivery(int(item["Id"]), "google", str(connection["id"]), "sent", "")


def _deliver_microsoft(repository, item, match, connection, flags, post, request) -> None:
    body = microsoft_event_body(match, flags.public_web_url)
    digest = content_hash({"event": body, "void": bool(match.get("void"))})
    current = repository.event_map(int(connection["id"]), int(match["id"]))
    if current and current["content_hash"] == digest:
        telemetry.count("delivery.skipped", "microsoft", "unchanged")
        repository.record_delivery(int(item["Id"]), "microsoft", str(connection["id"]), "skipped", "unchanged")
        return
    refresh = decrypt_secret(flags.token_key, connection["refresh_token"])
    access = refresh_access_token("microsoft", flags, refresh, post)
    external_id = upsert_microsoft_event(
        access,
        match,
        flags.public_web_url,
        current["external_event_id"] if current else None,
        request,
    )
    if not external_id:
        repository.record_delivery(int(item["Id"]), "microsoft", str(connection["id"]), "skipped", "nothing to remove")
        return
    repository.save_event_map(int(connection["id"]), int(match["id"]), external_id, digest)
    telemetry.count("delivery.sent", "microsoft")
    repository.record_delivery(int(item["Id"]), "microsoft", str(connection["id"]), "sent", "")


def drain_once(flags: NotifyFlags | None = None, post=None, request=None) -> int:
    flags = flags or load_flags()
    if not flags.worker_enabled or not flags.delivery_ready():
        return 0
    connection = Database.get_connection()
    try:
        repository = NotifyRepository(connection)
        if flags.push_ready():
            try:
                enqueue_reminders(repository, league_today())
            except Exception:
                logger.exception("match-day reminder sweep failed")
        rows = repository.claim_pending()
        for item in rows:
            try:
                deliver_item(repository, item, flags, post or _post, request or _request)
            except Exception as exc:
                # The type only: a provider or push error message can echo a body or an endpoint.
                logger.error("match notice %s failed error=%s", item.get("Id"), type(exc).__name__)
                try:
                    repository.mark_retry(int(item["Id"]), int(item.get("Attempts") or 1), "delivery failed")
                except Exception:
                    logger.exception("could not record a retry for notice %s", item.get("Id"))
        return len(rows)
    finally:
        connection.close()


async def run_notify_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(drain_once)
        except Exception:
            logger.exception("notification worker failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=20)
        except asyncio.TimeoutError:
            continue
