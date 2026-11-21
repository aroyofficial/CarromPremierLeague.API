from __future__ import annotations

import uuid

from fastapi import HTTPException

from schemas.auth_schema import AdminResponse
from schemas.webhook_schema import (
    WebhookCreatedResponse,
    WebhookCreateRequest,
    WebhookDeliveryResponse,
    WebhookEventOption,
    WebhookListResponse,
    WebhookResponse,
    WebhookTestResponse,
    WebhookUpdateRequest,
)
from services.webhooks.events import (
    ALL_WEBHOOK_EVENTS,
    TEST_EVENT,
    WEBHOOK_EVENTS,
    build_envelope,
    ordered_webhook_events,
)
from services.webhooks.security import (
    WebhookTargetError,
    generate_signing_secret,
    validate_webhook_url,
)
from services.webhooks.worker import send_signed_webhook


def available_webhook_events() -> list[WebhookEventOption]:
    return [
        WebhookEventOption(
            code=item.code,
            group=item.group,
            name=item.name,
            description=item.description,
        )
        for item in WEBHOOK_EVENTS
    ]


class WebhookService:
    def __init__(self, repository, post=None):
        self.repository = repository
        self._post = post

    @staticmethod
    def _validate_events(events: list[str]) -> list[str]:
        cleaned = [e.strip() for e in events if isinstance(e, str) and e.strip()]
        if not cleaned:
            raise HTTPException(status_code=400, detail="Choose at least one event for this webhook.")
        unknown = sorted({code for code in cleaned if code not in ALL_WEBHOOK_EVENTS})
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown webhook event: {', '.join(unknown)}.")
        return ordered_webhook_events(cleaned)

    @staticmethod
    def _validate_url(url: str) -> str:
        try:
            return validate_webhook_url(url, resolve_dns=False)
        except WebhookTargetError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @staticmethod
    def _to_response(record: dict) -> WebhookResponse:
        return WebhookResponse(
            id=int(record["id"]),
            name=record["name"],
            url=record["url"],
            description=record.get("description"),
            secret_hint=record["secret_hint"],
            events=ordered_webhook_events(record.get("events", [])),
            is_active=bool(record.get("is_active", True)),
            failure_count=int(record.get("failure_count") or 0),
            last_delivered_at=record.get("last_delivered_at"),
            last_status_code=record.get("last_status_code"),
            last_error=record.get("last_error"),
            created_at=record.get("created_at"),
            updated_at=record.get("updated_at"),
            created_by=int(record["created_by"]),
        )

    def list_webhooks(self) -> WebhookListResponse:
        rows = self.repository.list_all()
        return WebhookListResponse(
            webhooks=[self._to_response(row) for row in rows],
            available_events=available_webhook_events(),
        )

    def create_webhook(self, request: WebhookCreateRequest, actor: AdminResponse) -> WebhookCreatedResponse:
        url = self._validate_url(request.url)
        events = self._validate_events(request.events)
        raw_secret, sealed, hint = generate_signing_secret()
        created = self.repository.create(
            name=request.name,
            url=url,
            description=request.description,
            secret_ciphertext=sealed,
            secret_hint=hint,
            events=events,
            is_active=request.is_active,
            created_by=actor.id,
        )
        base = self._to_response(created)
        return WebhookCreatedResponse(**base.model_dump(), signing_secret=raw_secret)

    def update_webhook(self, webhook_id: int, request: WebhookUpdateRequest) -> WebhookResponse:
        existing = self.repository.get_by_id(webhook_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Webhook not found.")
        changes = request.model_dump(exclude_unset=True)
        if "url" in changes:
            if changes["url"] is None:
                changes.pop("url")
            else:
                changes["url"] = self._validate_url(changes["url"])
        if "events" in changes:
            if changes["events"] is None:
                changes.pop("events")
            else:
                changes["events"] = self._validate_events(changes["events"])
        updated = self.repository.update(webhook_id, changes)
        if not updated:
            raise HTTPException(status_code=404, detail="Webhook not found.")
        return self._to_response(updated)

    def rotate_secret(self, webhook_id: int) -> WebhookCreatedResponse:
        existing = self.repository.get_by_id(webhook_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Webhook not found.")
        raw_secret, sealed, hint = generate_signing_secret()
        rotated = self.repository.rotate_secret(webhook_id, sealed, hint)
        if not rotated:
            raise HTTPException(status_code=404, detail="Webhook not found.")
        base = self._to_response(rotated)
        return WebhookCreatedResponse(**base.model_dump(), signing_secret=raw_secret)

    def delete_webhook(self, webhook_id: int) -> bool:
        if not self.repository.delete(webhook_id):
            raise HTTPException(status_code=404, detail="Webhook not found.")
        return True

    def list_deliveries(self, webhook_id: int, limit: int = 25) -> list[WebhookDeliveryResponse]:
        existing = self.repository.get_by_id(webhook_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Webhook not found.")
        rows = self.repository.list_deliveries(webhook_id, limit=limit)
        return [WebhookDeliveryResponse(**row) for row in rows]

    def send_test(self, webhook_id: int, actor: AdminResponse) -> WebhookTestResponse:
        existing = self.repository.get_by_id(webhook_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Webhook not found.")
        event_id, payload_json = build_envelope(
            TEST_EVENT,
            {
                "webhook_id": existing["id"],
                "webhook_name": existing["name"],
                "triggered_by": actor.username,
                "message": "Test ping from Carrom Premier League.",
            },
        )
        delivery_id = self.repository.enqueue_delivery(
            webhook_id=int(existing["id"]),
            event_id=event_id,
            event_type=TEST_EVENT,
            idempotency_key=f"test:{uuid.uuid4().hex}",
            payload=payload_json,
            status="leased",
            attempts=1,
        )
        outcome = send_signed_webhook(
            url=existing["url"],
            secret_ciphertext=existing["secret_ciphertext"],
            event_id=event_id,
            event_type=TEST_EVENT,
            payload_json=payload_json,
            post=self._post,
        )
        if outcome["ok"]:
            self.repository.mark_sent(
                int(delivery_id),
                int(existing["id"]),
                int(outcome["status_code"]),
                int(outcome["duration_ms"]),
            )
        else:
            self.repository.mark_failed(
                int(delivery_id),
                int(existing["id"]),
                attempts=1,
                error_detail=outcome["error"] or "Test delivery failed.",
                status_code=outcome["status_code"],
                duration_ms=outcome["duration_ms"],
                terminal=True,
            )
        return WebhookTestResponse(
            delivery_id=int(delivery_id),
            event_id=event_id,
            ok=bool(outcome["ok"]),
            status_code=outcome["status_code"],
            duration_ms=int(outcome["duration_ms"]),
            error=outcome["error"],
        )
