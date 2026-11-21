from fastapi import APIRouter, Depends, Request, Response, status

from core.database import get_db
from core.permissions import MANAGE_WIDGETS
from core.response import ApiResponse
from repositories.widget_repository import WidgetRepository
from routes.auth_routes import require_permission
from schemas.auth_schema import AdminResponse
from schemas.widget_schema import (
    WidgetCreatedResponse,
    WidgetCreateRequest,
    WidgetListResponse,
    WidgetResponse,
    WidgetUpdateRequest,
)
from services.widget_service import DbWidgetSnapshotSource, WidgetService

router = APIRouter(prefix="/widgets", tags=["Widgets"])


def get_service(request: Request, db=Depends(get_db)) -> WidgetService:
    injected = getattr(request.app.state, "widget_service", None)
    if injected is not None:
        return injected
    return WidgetService(WidgetRepository(db), DbWidgetSnapshotSource(db))


@router.get(
    "",
    response_model=ApiResponse[WidgetListResponse],
    dependencies=[Depends(require_permission(MANAGE_WIDGETS))],
    summary="List live stream widgets",
)
def list_widgets(service: WidgetService = Depends(get_service)):
    return ApiResponse(success=True, message="Widgets fetched successfully.", data=service.list_widgets())


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[WidgetCreatedResponse],
    summary="Create a private widget link and return its token once",
)
def create_widget(
    body: WidgetCreateRequest,
    actor: AdminResponse = Depends(require_permission(MANAGE_WIDGETS)),
    service: WidgetService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Widget created. Copy the link now; it will not be shown again.",
        data=service.create(body, actor),
    )


@router.patch(
    "/{widget_id}",
    response_model=ApiResponse[WidgetResponse],
    dependencies=[Depends(require_permission(MANAGE_WIDGETS))],
    summary="Rename a widget, turn its link on or off, or change its expiry",
)
def update_widget(widget_id: int, body: WidgetUpdateRequest, service: WidgetService = Depends(get_service)):
    return ApiResponse(success=True, message="Widget updated successfully.", data=service.update(widget_id, body))


@router.post(
    "/{widget_id}/rotate",
    response_model=ApiResponse[WidgetCreatedResponse],
    dependencies=[Depends(require_permission(MANAGE_WIDGETS))],
    summary="Issue a new token; the previous link stops working",
)
def rotate_widget(widget_id: int, service: WidgetService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="Widget link rotated. Copy the new link now; the previous link no longer works.",
        data=service.rotate(widget_id),
    )


@router.get(
    "/t/{token}",
    response_model=ApiResponse[dict],
    summary="Live data for a private widget link (no sign-in)",
)
def widget_snapshot(token: str, response: Response, service: WidgetService = Depends(get_service)):
    response.headers["Cache-Control"] = "no-store"
    return ApiResponse(success=True, message="Widget fetched successfully.", data=service.snapshot(token))
