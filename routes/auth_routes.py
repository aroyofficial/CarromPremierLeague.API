from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.database import get_db
from core.response import ApiResponse
from repositories.admin_repository import AdminRepository
from schemas.auth_schema import (
    AdminCreateRequest,
    AdminResponse,
    AdminUpdateRequest,
    LoginRequest,
    TokenResponse,
)
from services.auth_service import AuthService


router = APIRouter(tags=["Authentication"])
_bearer_scheme = HTTPBearer(auto_error=False)


def get_service(db=Depends(get_db)) -> AuthService:
    return AuthService(AdminRepository(db))


def get_current_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    service: AuthService = Depends(get_service),
) -> AdminResponse:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return service.current_admin(credentials.credentials)


@router.post("/auth/login", response_model=ApiResponse[TokenResponse], summary="Admin login")
def login(request: LoginRequest, service: AuthService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="Login successful.",
        data=service.login(request),
    )


@router.post(
    "/auth/bootstrap",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AdminResponse],
    summary="Create the first admin",
)
def bootstrap_admin(request: AdminCreateRequest, service: AuthService = Depends(get_service)):
    return ApiResponse(
        success=True,
        message="Initial admin created successfully.",
        data=service.bootstrap_admin(request),
    )


@router.get("/auth/me", response_model=ApiResponse[AdminResponse], summary="Current admin")
def current_admin(admin: AdminResponse = Depends(get_current_admin)):
    return ApiResponse(success=True, message="Current admin fetched successfully.", data=admin)


@router.get("/admins", response_model=ApiResponse[list[AdminResponse]], summary="List admins")
def list_admins(
    _: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Admins fetched successfully.", data=service.list_admins())


@router.get("/admins/{admin_id}", response_model=ApiResponse[AdminResponse], summary="Get admin")
def get_admin(
    admin_id: int,
    _: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Admin fetched successfully.", data=service.get_admin(admin_id))


@router.post(
    "/admins",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiResponse[AdminResponse],
    summary="Create admin",
)
def create_admin(
    request: AdminCreateRequest,
    _: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Admin created successfully.", data=service.create_admin(request))


@router.patch("/admins/{admin_id}", response_model=ApiResponse[AdminResponse], summary="Update admin")
def update_admin(
    admin_id: int,
    request: AdminUpdateRequest,
    current: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Admin updated successfully.",
        data=service.update_admin(admin_id, request, current),
    )


@router.delete("/admins/{admin_id}", response_model=ApiResponse[bool], summary="Delete admin")
def delete_admin(
    admin_id: int,
    current: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Admin deleted successfully.",
        data=service.delete_admin(admin_id, current),
    )
