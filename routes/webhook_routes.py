from fastapi import APIRouter, Depends, Request, status

from core.database import get_db
from core.permissions import MANAGE_WEBHOOKS
from core.response import ApiResponse
from repositories.webhook_repository import WebhookRepository
from routes.auth_routes import require_permission
from schemas.auth_schema import AdminResponse
from schemas.webhook_schema import (
    WebhookCreatedResponse,
    WebhookCreateRequest,
    WebhookDeliveryResponse,
    WebhookListResponse,
    WebhookResponse,
    WebhookTestResponse,
    WebhookUpdateRequest,
)
from services.webhooks.service import WebhookService

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def get_service(request: Request, db=Depends(get_db)) -> WebhookService:
    injected = getattr(request.app.state, "webhook_service", None)
    if injected is not None:
        return injected
    return WebhookService(WebhookRepository(db))


@router.get(
    "",
    response_model=ApiResponse[WebhookListResponse],
    dependencies=[Depends(require_permission(MANAGE_WEBHOOKS))],
    summary="List registered webhooks and available events",
)
def list_webhooks(service: WebhookService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="Webhooks fetched successfully.",
        data=service.list_webhooks(),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[WebhookCreatedResponse],
    summary="Register a webhook and issue a one-time signing secret",
)
def create_webhook(
    body: WebhookCreateRequest,
    actor: AdminResponse = Depends(require_permission(MANAGE_WEBHOOKS)),
    service: WebhookService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Webhook created. Copy the signing secret now; it will not be shown again.",
        data=service.create_webhook(body, actor),
    )


@router.patch(
    "/{webhook_id}",
    response_model=ApiResponse[WebhookResponse],
    dependencies=[Depends(require_permission(MANAGE_WEBHOOKS))],
    summary="Update a webhook's name, URL, subscribed events or active state",
)
def update_webhook(
    webhook_id: int,
    body: WebhookUpdateRequest,
    service: WebhookService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Webhook updated successfully.",
        data=service.update_webhook(webhook_id, body),
    )


@router.post(
    "/{webhook_id}/rotate-secret",
    response_model=ApiResponse[WebhookCreatedResponse],
    dependencies=[Depends(require_permission(MANAGE_WEBHOOKS))],
    summary="Rotate a webhook's signing secret",
)
def rotate_webhook_secret(webhook_id: int, service: WebhookService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="Signing secret rotated. Copy the new secret now.",
        data=service.rotate_secret(webhook_id),
    )


@router.post(
    "/{webhook_id}/test",
    response_model=ApiResponse[WebhookTestResponse],
    summary="Send a signed test ping to a webhook",
)
def test_webhook(
    webhook_id: int,
    actor: AdminResponse = Depends(require_permission(MANAGE_WEBHOOKS)),
    service: WebhookService = Depends(get_service),
):
    result = service.send_test(webhook_id, actor)
    message = (
        f"Test ping delivered (HTTP {result.status_code})."
        if result.ok
        else f"Test ping failed ({result.error or 'no response'})."
    )
    return ApiResponse(success=True, message=message, data=result)


@router.get(
    "/{webhook_id}/deliveries",
    response_model=ApiResponse[list[WebhookDeliveryResponse]],
    dependencies=[Depends(require_permission(MANAGE_WEBHOOKS))],
    summary="List recent delivery attempts for a webhook",
)
def list_webhook_deliveries(
    webhook_id: int,
    limit: int = 25,
    service: WebhookService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Webhook deliveries fetched successfully.",
        data=service.list_deliveries(webhook_id, limit=limit),
    )


@router.delete(
    "/{webhook_id}",
    response_model=ApiResponse[bool],
    dependencies=[Depends(require_permission(MANAGE_WEBHOOKS))],
    summary="Delete a webhook",
)
def delete_webhook(webhook_id: int, service: WebhookService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="Webhook deleted.",
        data=service.delete_webhook(webhook_id),
    )
