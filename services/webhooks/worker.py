from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone

from core.database import Database
from repositories.webhook_repository import WebhookRepository
from services.webhooks.security import (
    WebhookTargetError,
    sign_webhook_payload,
    unseal_signing_secret,
    validate_webhook_url,
)

logger = logging.getLogger(__name__)


def webhooks_worker_enabled() -> bool:
    raw = os.getenv("CPL_WEBHOOK_WORKER_ENABLED")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() in {"1", "true", "yes"}


def _default_post(url: str, content: bytes, headers: dict[str, str]):
    import httpx

    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        return client.post(url, content=content, headers=headers)


def send_signed_webhook(
    *,
    url: str,
    secret_ciphertext: str,
    event_id: str,
    event_type: str,
    payload_json: str,
    post=None,
    timestamp: int | None = None,
) -> dict:
    started = time.perf_counter()
    try:
        checked_url = validate_webhook_url(url, resolve_dns=True)
    except WebhookTargetError as exc:
        elapsed = max(0, int((time.perf_counter() - started) * 1000))
        return {
            "ok": False,
            "status_code": None,
            "duration_ms": elapsed,
            "error": str(exc),
            "terminal": True,
        }

    try:
        secret = unseal_signing_secret(secret_ciphertext)
    except Exception:
        elapsed = max(0, int((time.perf_counter() - started) * 1000))
        return {
            "ok": False,
            "status_code": None,
            "duration_ms": elapsed,
            "error": "Stored webhook signing secret could not be read.",
            "terminal": True,
        }

    ts = int(timestamp if timestamp is not None else datetime.now(timezone.utc).timestamp())
    signature = sign_webhook_payload(secret, ts, payload_json)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "CPL-Webhooks/1.0",
        "X-CPL-Event": event_type,
        "X-CPL-Delivery": event_id,
        "X-CPL-Timestamp": str(ts),
        "X-CPL-Signature-256": signature,
    }

    sender = post or _default_post
    try:
        response = sender(checked_url, payload_json.encode("utf-8"), headers)
        elapsed = max(0, int((time.perf_counter() - started) * 1000))
        code = int(response.status_code)
        if 200 <= code < 300:
            return {
                "ok": True,
                "status_code": code,
                "duration_ms": elapsed,
                "error": None,
                "terminal": False,
            }
        # 4xx (except 408/429) will not succeed on retry with the same payload.
        terminal = 400 <= code < 500 and code not in {408, 429}
        return {
            "ok": False,
            "status_code": code,
            "duration_ms": elapsed,
            "error": f"HTTP {code}",
            "terminal": terminal,
        }
    except Exception as exc:
        elapsed = max(0, int((time.perf_counter() - started) * 1000))
        return {
            "ok": False,
            "status_code": None,
            "duration_ms": elapsed,
            "error": f"network_error: {type(exc).__name__}",
            "terminal": False,
        }


def deliver_item(repository: WebhookRepository, item: dict, post=None) -> None:
    delivery_id = int(item["Id"])
    webhook_id = int(item["WebhookId"])
    attempts = int(item.get("Attempts") or 1)
    void = bool(item.get("Void"))
    active = bool(item.get("IsActive", True))
    if void or not active:
        repository.mark_failed(
            delivery_id,
            webhook_id,
            attempts=attempts,
            error_detail="Webhook is inactive or deleted.",
            terminal=True,
        )
        return

    outcome = send_signed_webhook(
        url=item["Url"],
        secret_ciphertext=item["SecretCiphertext"],
        event_id=item["EventId"],
        event_type=item["EventType"],
        payload_json=item["Payload"],
        post=post,
    )
    if outcome["ok"]:
        repository.mark_sent(
            delivery_id,
            webhook_id,
            int(outcome["status_code"]),
            int(outcome["duration_ms"]),
        )
    else:
        repository.mark_failed(
            delivery_id,
            webhook_id,
            attempts=attempts,
            error_detail=outcome["error"] or "Delivery failed.",
            status_code=outcome["status_code"],
            duration_ms=outcome["duration_ms"],
            terminal=bool(outcome["terminal"]),
        )


def drain_webhooks_once(post=None) -> int:
    if not webhooks_worker_enabled():
        return 0
    connection = Database.get_connection()
    try:
        repository = WebhookRepository(connection)
        rows = repository.claim_pending()
        for item in rows:
            try:
                deliver_item(repository, item, post=post)
            except Exception as exc:
                logger.error("webhook delivery %s failed error=%s", item.get("Id"), type(exc).__name__)
                try:
                    repository.mark_failed(
                        int(item["Id"]),
                        int(item["WebhookId"]),
                        attempts=int(item.get("Attempts") or 1),
                        error_detail=f"unexpected: {type(exc).__name__}",
                    )
                except Exception:
                    logger.exception("could not record retry for webhook delivery %s", item.get("Id"))
        return len(rows)
    except Exception:
        # Table may not exist yet before DB migration is applied.
        logger.debug("webhook outbox poll skipped", exc_info=True)
        return 0
    finally:
        connection.close()


async def run_webhook_worker(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(drain_webhooks_once)
        except Exception:
            logger.exception("webhook worker failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except asyncio.TimeoutError:
            continue
