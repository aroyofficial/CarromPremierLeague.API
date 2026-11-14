from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.database import get_db
from core.permissions import (
    AREA_NAMES,
    AREAS,
    CATALOG,
    CREATE_ADMINS,
    DELETE_ADMINS,
    MANAGE_PERMISSIONS,
    ROLES,
    SECTIONS,
    VIEW_ADMINS,
    denied_message,
    ensure_known,
    ordered,
)
from core.response import ApiResponse
from repositories.admin_repository import AdminRepository
from repositories.permission_repository import PermissionRepository
from schemas.auth_schema import (
    AdminCreateRequest,
    AdminPermissionsUpdate,
    AdminResponse,
    AdminUpdateRequest,
    LoginRequest,
    PermissionAreaNode,
    PermissionNode,
    PermissionResponse,
    PermissionRoleResponse,
    PermissionSectionNode,
    PermissionTreeResponse,
    TokenResponse,
)
from services.auth_service import AuthService


router = APIRouter(tags=["Authentication"])
_bearer_scheme = HTTPBearer(auto_error=False)


def get_service(db=Depends(get_db)) -> AuthService:
    return AuthService(AdminRepository(db), PermissionRepository(db))


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


def require_permission(*codes: str):
    """Dependency that passes when the signed-in admin holds at least one of the codes."""
    if not codes:
        raise ValueError("require_permission needs at least one permission code.")
    ensure_known(*codes)

    def dependency(admin: AdminResponse = Depends(get_current_admin)) -> AdminResponse:
        if not any(code in admin.permissions for code in codes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denied_message(codes))
        return admin

    dependency.permission_codes = codes
    dependency.__name__ = "require_" + "_or_".join(code.replace(".", "_") for code in codes)
    return dependency


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


@router.get("/permissions", response_model=ApiResponse[list[PermissionResponse]], summary="Permission catalog")
def list_permissions(_: AdminResponse = Depends(get_current_admin)):
    return ApiResponse(
        success=True,
        message="Permissions fetched successfully.",
        data=[
            PermissionResponse(
                code=permission.code,
                area=AREA_NAMES[permission.area],
                name=permission.name,
                description=permission.description,
                area_code=permission.area,
                parent=permission.parent,
            )
            for permission in CATALOG
        ],
    )


def build_permission_tree() -> PermissionTreeResponse:
    def area_node(area) -> PermissionAreaNode:
        return PermissionAreaNode(
            code=area.code,
            name=area.name,
            description=area.description,
            icon=area.icon,
            permissions=[
                PermissionNode(
                    code=permission.code,
                    name=permission.name,
                    description=permission.description,
                    parent=permission.parent,
                )
                for permission in CATALOG
                if permission.area == area.code
            ],
        )

    return PermissionTreeResponse(
        sections=[
            PermissionSectionNode(
                code=section.code,
                name=section.name,
                description=section.description,
                areas=[area_node(area) for area in AREAS if area.section == section.code],
            )
            for section in SECTIONS
        ],
        roles=[
            PermissionRoleResponse(
                code=role.code,
                name=role.name,
                description=role.description,
                permissions=ordered(role.permissions),
            )
            for role in ROLES
        ],
    )


@router.get(
    "/permissions/tree",
    response_model=ApiResponse[PermissionTreeResponse],
    summary="Permission hierarchy and roles",
)
def permission_tree(_: AdminResponse = Depends(get_current_admin)):
    return ApiResponse(success=True, message="Permission hierarchy fetched successfully.", data=build_permission_tree())


@router.get("/admins", response_model=ApiResponse[list[AdminResponse]], summary="List admins")
def list_admins(
    _: AdminResponse = Depends(require_permission(VIEW_ADMINS, MANAGE_PERMISSIONS)),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Admins fetched successfully.", data=service.list_admins())


@router.get("/admins/{admin_id}", response_model=ApiResponse[AdminResponse], summary="Get admin")
def get_admin(
    admin_id: int,
    _: AdminResponse = Depends(require_permission(VIEW_ADMINS, MANAGE_PERMISSIONS)),
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
    current: AdminResponse = Depends(require_permission(CREATE_ADMINS)),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Admin created successfully.",
        data=service.create_admin(request, current),
    )


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


@router.put(
    "/admins/{admin_id}/permissions",
    response_model=ApiResponse[AdminResponse],
    summary="Set admin permissions",
)
def set_admin_permissions(
    admin_id: int,
    request: AdminPermissionsUpdate,
    current: AdminResponse = Depends(require_permission(MANAGE_PERMISSIONS)),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Permissions saved successfully.",
        data=service.set_permissions(admin_id, request, current),
    )


@router.delete("/admins/{admin_id}", response_model=ApiResponse[bool], summary="Delete admin")
def delete_admin(
    admin_id: int,
    current: AdminResponse = Depends(require_permission(DELETE_ADMINS)),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Admin deleted successfully.",
        data=service.delete_admin(admin_id, current),
    )
