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
from datetime import date, datetime, timezone

from core.database import Database
from repositories.notify_repository import NotifyRepository
from services.match_rules import league_today
from services.notify.delivery import delivery_plan
from services.notify.events import CANCELLED, SCHEDULE, SYNC_PROBLEM, WITHDRAW
from services.notify.flags import NotifyFlags, load_flags
from services.notify.providers import (
    ProviderError,
    ReauthorizationRequired,
    content_hash,
    delete_google_event,
    delete_microsoft_event,
    google_event_body,
    microsoft_event_body,
    refresh_access_token,
    upsert_google_event,
    upsert_microsoft_event,
)
from services.notify import telemetry
from services.notify.push import push_payload, send_web_push
from services.notify.security import StateError, decrypt_secret

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
    pushable = current is not None or event in (CANCELLED, SCHEDULE, SYNC_PROBLEM)
    subscriptions = repository.active_subscriptions() if flags.push_ready() and pushable else []
    connections = repository.active_connections() if flags.google_ready() or flags.microsoft_ready() else []
    if event == SYNC_PROBLEM:
        # Only the admin who owns the connection hears about it, and no calendar is written.
        owner = int(payload.get("admin_id") or 0)
        subscriptions = [row for row in subscriptions if row.get("admin_id") == owner]
        connections = []
    if event != WITHDRAW:
        # The admin removed this match from these calendars. Only the withdraw or the add-back touches them.
        withdrawn = repository.withdrawn_connection_ids(int(item["MatchId"]))
        connections = [row for row in connections if int(row["id"]) not in withdrawn]
    existing_only = bool(payload.get("existing_only"))
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
        telemetry.count("delivery.attempt", channel, event)
        try:
            if channel == "push":
                _deliver_push(repository, item, snapshot, before, action["subscription"], flags)
            elif channel == "google":
                _deliver_google(repository, item, live, action["connection"], flags, post, request,
                                withdraw=event == WITHDRAW, existing_only=existing_only)
            elif channel == "microsoft":
                _deliver_microsoft(repository, item, live, action["connection"], flags, post, request,
                                   withdraw=event == WITHDRAW, existing_only=existing_only)
        except StateError:
            # The stored grant cannot be decrypted, for example after the token key changed.
            # Retrying cannot fix it, so the connection waits for the admin to connect again.
            repository.set_connection_status(int(action["connection"]["id"]), "revoked")
            telemetry.count("connection.revoked", channel)
            telemetry.count("delivery.failed", channel, "token_unreadable")
            _queue_sync_problem(repository, action["connection"], channel)
            repository.record_delivery(int(item["Id"]), channel, str(action.get("target") or ""), "failed", "token_unreadable")
        except ProviderError as exc:
            # A revoked grant stops that connection. Only the admin can fix it, so nothing retries.
            if isinstance(exc, ReauthorizationRequired):
                repository.set_connection_status(int(action["connection"]["id"]), "revoked")
                telemetry.count("connection.revoked", channel)
                _queue_sync_problem(repository, action["connection"], channel)
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
        _observe_age(item)
        repository.mark_done(int(item["Id"]))


def _observe_age(item: dict, name: str = "outbox.age_at_done") -> None:
    # How long the notice waited from queueing to this point. Its delivery latency for the worker.
    created = item.get("CreatedAt")
    if not isinstance(created, datetime):
        return
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    telemetry.observe(name, (datetime.now(timezone.utc) - created).total_seconds())


def _note_calendar_write(item: dict, channel: str, match: dict, existed: bool) -> None:
    outcome = "cancelled" if match.get("void") else ("updated" if existed else "created")
    telemetry.count("calendar.event", channel, outcome)
    _observe_age(item, "calendar.sync_latency")


def _queue_sync_problem(repository, connection: dict, channel: str) -> None:
    # The admin's own devices hear once that the connection needs them. A failure here must not
    # stop the revoke from being recorded, so it is logged and the delivery carries on.
    admin_id = connection.get("admin_id")
    if admin_id is None:
        return
    try:
        repository.enqueue_sync_problem(int(connection["id"]), int(admin_id), channel)
    except Exception:
        logger.exception("could not queue the calendar sync-problem notice")


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
        telemetry.count("push.accepted", None, item["EventType"])
        repository.note_push_success(int(subscription["id"]))
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


def _withdraw(repository, item, connection, match_id: int, channel: str, flags, post, delete) -> None:
    """Remove this match's event from one calendar. The map keeps the id and stays withdrawn."""
    if int(connection["id"]) not in repository.withdrawn_connection_ids(int(match_id)):
        # The admin added the match back after this removal was queued. A late retry must not delete it.
        repository.record_delivery(int(item["Id"]), channel, str(connection["id"]), "skipped", "restored")
        return
    current = repository.event_map(int(connection["id"]), int(match_id))
    if not current or not (current["external_event_id"] or "").strip():
        repository.record_delivery(int(item["Id"]), channel, str(connection["id"]), "skipped", "nothing to remove")
        return
    refresh = decrypt_secret(flags.token_key, connection["refresh_token"])
    access = refresh_access_token(channel, flags, refresh, post)
    delete(access, current["external_event_id"])
    telemetry.count("calendar.withdrawn", channel)
    _observe_age(item, "calendar.sync_latency")
    repository.record_delivery(int(item["Id"]), channel, str(connection["id"]), "sent", "withdrawn")


def _not_on_calendar(repository, item, channel: str, connection, current) -> bool:
    if current and (current["external_event_id"] or "").strip():
        return False
    repository.record_delivery(int(item["Id"]), channel, str(connection["id"]), "skipped", "not on this calendar")
    telemetry.count("delivery.skipped", channel, "not_on_calendar")
    return True


def _deliver_google(repository, item, match, connection, flags, post, request, withdraw: bool = False,
                    existing_only: bool = False) -> None:
    if withdraw:
        _withdraw(
            repository, item, connection, int(match["id"]), "google", flags, post,
            lambda access, event_id: delete_google_event(access, event_id, request),
        )
        return
    body = google_event_body(match, flags.public_web_url)
    digest = content_hash(body)
    current = repository.event_map(int(connection["id"]), int(match["id"]))
    if existing_only and _not_on_calendar(repository, item, "google", connection, current):
        return
    if current and current["content_hash"] == digest:
        telemetry.count("delivery.skipped", "google", "unchanged")
        repository.record_delivery(int(item["Id"]), "google", str(connection["id"]), "skipped", "unchanged")
        return
    refresh = decrypt_secret(flags.token_key, connection["refresh_token"])
    access = refresh_access_token("google", flags, refresh, post)
    external_id = upsert_google_event(access, match, flags.public_web_url, request)
    repository.save_event_map(int(connection["id"]), int(match["id"]), external_id, digest)
    telemetry.count("delivery.sent", "google")
    _note_calendar_write(item, "google", match, current is not None)
    repository.record_delivery(int(item["Id"]), "google", str(connection["id"]), "sent", "")


def _deliver_microsoft(repository, item, match, connection, flags, post, request, withdraw: bool = False,
                       existing_only: bool = False) -> None:
    if withdraw:
        _withdraw(
            repository, item, connection, int(match["id"]), "microsoft", flags, post,
            lambda access, event_id: delete_microsoft_event(access, event_id, request),
        )
        return
    body = microsoft_event_body(match, flags.public_web_url)
    digest = content_hash({"event": body, "void": bool(match.get("void"))})
    current = repository.event_map(int(connection["id"]), int(match["id"]))
    if existing_only and _not_on_calendar(repository, item, "microsoft", connection, current):
        return
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
    _note_calendar_write(item, "microsoft", match, current is not None)
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
