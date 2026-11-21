from fastapi import APIRouter, Depends, HTTPException, Request, status

from core.database import get_db
from core.permissions import MANAGE_APPS
from core.response import ApiResponse
from repositories.app_repository import AppRepository
from routes.auth_routes import require_permission
from schemas.app_schema import (
    AppCreatedResponse,
    AppCreateRequest,
    AppIdentityResponse,
    AppListResponse,
    AppResponse,
    AppUpdateRequest,
)
from schemas.auth_schema import AdminResponse
from services.app_service import AppService

router = APIRouter(prefix="/apps", tags=["Apps"])


def get_app_service(request: Request) -> AppService:
    injected = getattr(request.app.state, "app_service", None)
    if injected is not None:
        return injected
    db = next(get_db())
    # Note: resolved via FastAPI generator dependency below in production routes.
    return AppService(AppRepository(db))


def get_service(request: Request, db=Depends(get_db)) -> AppService:
    injected = getattr(request.app.state, "app_service", None)
    if injected is not None:
        return injected
    return AppService(AppRepository(db))


@router.get("/me", response_model=ApiResponse[AppIdentityResponse], summary="Verify API key and inspect granted scopes")
def current_app(request: Request):
    raw_key = request.headers.get("X-API-Key")
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Provide an X-API-Key header to inspect an app credential.",
        )
    injected = getattr(request.app.state, "app_service", None)
    if injected is not None:
        data = injected.identify(raw_key)
        return ApiResponse(success=True, message="API key verified.", data=data)
    from core.database import Database

    conn = Database.get_connection()
    try:
        data = AppService(AppRepository(conn)).identify(raw_key)
        return ApiResponse(success=True, message="API key verified.", data=data)
    finally:
        conn.close()


@router.get(
    "",
    response_model=ApiResponse[AppListResponse],
    dependencies=[Depends(require_permission(MANAGE_APPS))],
    summary="List registered apps and allowed permission scopes",
)
def list_apps(service: AppService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="Apps fetched successfully.",
        data=service.list_apps(),
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AppCreatedResponse],
    summary="Create an app and issue a one-time API key",
)
def create_app(
    body: AppCreateRequest,
    actor: AdminResponse = Depends(require_permission(MANAGE_APPS)),
    service: AppService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="App created. Copy the API key now; it will not be shown again.",
        data=service.create_app(body, actor),
    )


@router.patch(
    "/{app_id}",
    response_model=ApiResponse[AppResponse],
    summary="Update an app's name, scopes, expiration or active state",
)
def update_app(
    app_id: int,
    body: AppUpdateRequest,
    actor: AdminResponse = Depends(require_permission(MANAGE_APPS)),
    service: AppService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="App updated successfully.",
        data=service.update_app(app_id, body, actor),
    )


@router.post(
    "/{app_id}/rotate",
    response_model=ApiResponse[AppCreatedResponse],
    dependencies=[Depends(require_permission(MANAGE_APPS))],
    summary="Rotate an app's API key",
)
def rotate_app_key(app_id: int, service: AppService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="API key rotated. Copy the new key now; the previous key no longer works.",
        data=service.rotate_key(app_id),
    )


@router.delete(
    "/{app_id}",
    response_model=ApiResponse[bool],
    dependencies=[Depends(require_permission(MANAGE_APPS))],
    summary="Revoke an app API key",
)
def revoke_app(app_id: int, service: AppService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="App API key revoked.",
        data=service.revoke_app(app_id),
    )
