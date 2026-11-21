from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from core.database import get_db
from core.storage import MediaStorage, get_storage
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
    ChangePasswordRequest,
    EmailCodeRequest,
    EmailFactorRequest,
    ForgotPasswordRequest,
    LoginApproval,
    LoginRequest,
    LogoutRequest,
    MfaStatus,
    MfaTokenRequest,
    MfaVerifyRequest,
    PasskeyOptionsResponse,
    PasskeyRegisterRequest,
    PasswordCodeVerifyRequest,
    PasswordTicket,
    PendingLogin,
    PhoneCodeRequest,
    PhoneVerifyRequest,
    TotpConfirmRequest,
    TotpSetupResponse,
    ResetCodeRequest,
    ResetPasswordRequest,
    PermissionAreaNode,
    PermissionNode,
    PermissionResponse,
    PermissionRoleResponse,
    PermissionSectionNode,
    PermissionTreeResponse,
    TokenResponse,
)
from services.admin_codes import SqlCodeStore, _utcnow
from services.admin_mfa import SqlMfaStore
from services.auth_service import AuthService
from services.admin_sessions import SqlSessionStore
from services.mailer import get_mailer
from services.phone_otp import PhoneOtpService, SqlPhoneOtpStore, sender_from_settings
from services.media_service import MediaService


router = APIRouter(tags=["Authentication"])
_bearer_scheme = HTTPBearer(auto_error=False)


def get_service(db=Depends(get_db)) -> AuthService:
    return AuthService(
        AdminRepository(db),
        PermissionRepository(db),
        mailer=get_mailer(),
        codes=SqlCodeStore(db),
        sessions=SqlSessionStore(db),
        mfa=SqlMfaStore(db),
        phone_otp=PhoneOtpService(SqlPhoneOtpStore(db), sender_from_settings()),
    )


def get_admin_media(
    db=Depends(get_db),
    storage: Optional[MediaStorage] = Depends(get_storage),
) -> MediaService:
    return MediaService(storage, admin_repository=AdminRepository(db))


def _can_edit_admin(admin_id: int, current: AdminResponse) -> None:
    if current.id == admin_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Admins can change only their own profile and password.",
    )


def _pending_password_allowed(request: Request, admin: AdminResponse) -> bool:
    path = request.url.path.rstrip("/") or "/"
    if request.method == "GET" and path == "/auth/me":
        return True
    if path in {"/auth/login-request", "/auth/logout", "/auth/password-code", "/auth/password-code/verify", "/auth/change-password"}:
        return True
    if path.startswith("/auth/login-request/"):
        return True
    return request.method == "PATCH" and path == f"/admins/{admin.id}"


_API_KEY_FORBIDDEN_PREFIXES = (
    "/auth",
    "/admins",
    "/permissions",
    "/apps",
    "/webhooks",
    "/widgets",
    "/assistant",
    "/realtime",
    "/notifications",
    "/calendar/google",
    "/calendar/microsoft",
)


def _api_key_allowed_path(path: str) -> bool:
    normalized = (path or "/").rstrip("/") or "/"
    for prefix in _API_KEY_FORBIDDEN_PREFIXES:
        if normalized == prefix or normalized.startswith(f"{prefix}/"):
            return False
    return True


def _authenticate_api_key(request: Request, raw_key: str) -> AdminResponse:
    if not _api_key_allowed_path(request.url.path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API keys cannot access account or integration management endpoints.",
        )
    injected = getattr(request.app.state, "app_service", None)
    if injected is not None:
        return injected.authenticate_principal(raw_key)
    from core.database import Database
    from repositories.app_repository import AppRepository
    from services.app_service import AppService

    conn = Database.get_connection()
    try:
        return AppService(AppRepository(conn)).authenticate_principal(raw_key)
    finally:
        conn.close()


def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    service: AuthService = Depends(get_service),
) -> AdminResponse:
    api_key = request.headers.get("X-API-Key")
    if credentials is None and api_key:
        admin = _authenticate_api_key(request, api_key)
        request.state.api_key_principal = True
        return admin
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    admin = service.current_admin(credentials.credentials)
    if admin.must_change_password and not _pending_password_allowed(request, admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Change the temporary password to activate this account.",
        )
    return admin


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


@router.post("/auth/mfa/email-code", response_model=ApiResponse[bool], summary="Email a sign-in code")
def mfa_email_code(body: MfaTokenRequest, service: AuthService = Depends(get_service)):
    service.send_login_code(body.mfa_token)
    return ApiResponse(success=True, message="A sign-in code was sent to your email address.", data=True)


@router.post("/auth/mfa/passkey-options", response_model=ApiResponse[dict], summary="Passkey challenge for sign-in")
def mfa_passkey_options(body: MfaTokenRequest, request: Request, service: AuthService = Depends(get_service)):
    return ApiResponse(success=True, message=None, data=service.passkey_login_options(body.mfa_token, request.headers.get("origin")))


@router.post("/auth/mfa/verify", response_model=ApiResponse[TokenResponse], summary="Finish sign-in with a second factor")
def mfa_verify(body: MfaVerifyRequest, request: Request, service: AuthService = Depends(get_service)):
    data = service.verify_mfa(body, request.headers.get("origin"))
    return ApiResponse(success=True, message=None if data.pending else "Login successful.", data=data)


@router.post("/auth/password-code", response_model=ApiResponse[bool], summary="Email a code before a password change")
def password_code(admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    service.request_password_code(admin)
    return ApiResponse(success=True, message="A code was sent to your email address.", data=True)


@router.post(
    "/auth/password-code/verify",
    response_model=ApiResponse[PasswordTicket],
    summary="Check the emailed code and get a one-time password change ticket",
)
def verify_password_code(
    body: PasswordCodeVerifyRequest,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Code accepted.", data=service.verify_password_code(admin, body.code))


@router.post("/auth/change-password", response_model=ApiResponse[bool], summary="Set a new password with a change ticket")
def change_password(
    body: ChangePasswordRequest,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    service.change_password(admin, body)
    return ApiResponse(success=True, message="Password changed. Sign in again with the new password.", data=True)


@router.get("/auth/mfa", response_model=ApiResponse[MfaStatus], summary="Your sign-in factors")
def mfa_status(admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    return ApiResponse(success=True, message=None, data=service.mfa_status(admin))


@router.put("/auth/mfa/email", response_model=ApiResponse[MfaStatus], summary="Turn email sign-in codes on or off")
def mfa_email(body: EmailFactorRequest, admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    message = "Email sign-in codes are on." if body.enabled else "Email sign-in codes are off."
    return ApiResponse(success=True, message=message, data=service.set_email_factor(admin, body.enabled))


@router.post("/auth/mfa/totp", response_model=ApiResponse[TotpSetupResponse], summary="Start authenticator app setup")
def mfa_totp_start(admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    return ApiResponse(success=True, message=None, data=service.start_totp(admin))


@router.post("/auth/mfa/totp/confirm", response_model=ApiResponse[MfaStatus], summary="Confirm authenticator app setup")
def mfa_totp_confirm(
    body: TotpConfirmRequest,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Authenticator app added.", data=service.confirm_totp(admin, body.code))


@router.delete("/auth/mfa/totp", response_model=ApiResponse[MfaStatus], summary="Remove the authenticator app")
def mfa_totp_remove(admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    return ApiResponse(success=True, message="Authenticator app removed.", data=service.remove_totp(admin))


@router.post("/auth/mfa/passkeys/options", response_model=ApiResponse[PasskeyOptionsResponse], summary="Passkey challenge for setup")
def mfa_passkey_setup(request: Request, admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    return ApiResponse(success=True, message=None, data=service.passkey_registration_options(admin, request.headers.get("origin")))


@router.post("/auth/mfa/passkeys", response_model=ApiResponse[MfaStatus], summary="Save a passkey")
def mfa_passkey_save(
    body: PasskeyRegisterRequest,
    request: Request,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Passkey saved.", data=service.register_passkey(admin, body, request.headers.get("origin")))


@router.delete("/auth/mfa/passkeys/{passkey_id}", response_model=ApiResponse[MfaStatus], summary="Remove a passkey")
def mfa_passkey_remove(passkey_id: int, admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    return ApiResponse(success=True, message="Passkey removed.", data=service.remove_passkey(admin, passkey_id))


@router.post("/auth/logout", response_model=ApiResponse[bool], summary="End this admin session")
def logout(
    body: LogoutRequest | None = None,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    service.logout(admin, body.device_id if body else None)
    return ApiResponse(success=True, message="Logged out.", data=True)


@router.post("/auth/refresh", response_model=ApiResponse[TokenResponse], summary="Reissue the token with current claims")
def refresh_token(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    return ApiResponse(success=True, message="Token refreshed.", data=service.refresh(admin, credentials.credentials))


@router.get("/auth/login-request", response_model=ApiResponse[PendingLogin | None], summary="Sign-in waiting for this session")
def pending_login(admin: AdminResponse = Depends(get_current_admin), service: AuthService = Depends(get_service)):
    challenge = service.pending_login(admin)
    data = None if challenge is None else PendingLogin(
        request_id=challenge.id,
        device_label=challenge.device_label,
        expires_in=max(0, int((challenge.expires_at - _utcnow()).total_seconds())),
    )
    return ApiResponse(success=True, message=None, data=data)


@router.post("/auth/login-request/approve", response_model=ApiResponse[bool], summary="Approve a sign-in and log out here")
def approve_login(
    body: LoginApproval,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    service.approve_login(admin, body.request_id)
    return ApiResponse(success=True, message="The other device can sign in now.", data=True)


@router.post("/auth/login-request/deny", response_model=ApiResponse[bool], summary="Keep this session and refuse the other sign-in")
def deny_login(
    body: LoginApproval,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    service.deny_login(admin, body.request_id)
    return ApiResponse(success=True, message="The other sign-in was declined.", data=True)


@router.post("/auth/login-request/claim", response_model=ApiResponse[TokenResponse], summary="Collect a token after approval")
def claim_login(body: LoginApproval, service: AuthService = Depends(get_service)):
    data = service.claim_login(body.request_id)
    return ApiResponse(success=True, message=None if data.pending else "Login successful.", data=data)


@router.post("/auth/login-request/take", response_model=ApiResponse[TokenResponse], summary="Take the session from the other device")
def take_login(body: LoginApproval, service: AuthService = Depends(get_service)):
    data = service.take_login(body.request_id)
    return ApiResponse(success=True, message="This device is signed in.", data=data)


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


@router.post("/auth/forgot-password", response_model=ApiResponse[bool], summary="Email a password reset code")
def forgot_password(request: ForgotPasswordRequest, service: AuthService = Depends(get_service)):
    service.request_password_reset(request.username)
    return ApiResponse(
        success=True,
        message="If that username has an email address, a reset code is on its way.",
        data=True,
    )


@router.post(
    "/auth/reset-code/verify",
    response_model=ApiResponse[PasswordTicket],
    summary="Check a reset code and get a one-time reset ticket",
)
def verify_reset_code(request: ResetCodeRequest, service: AuthService = Depends(get_service)):
    return ApiResponse(success=True, message="Code accepted.", data=service.verify_reset_code(request))


@router.post("/auth/reset-password", response_model=ApiResponse[bool], summary="Reset a password with a ticket or code")
def reset_password(request: ResetPasswordRequest, service: AuthService = Depends(get_service)):
    service.reset_password(request)
    return ApiResponse(success=True, message="Password updated. You can sign in with it now.", data=True)


@router.get("/auth/me", response_model=ApiResponse[AdminResponse], summary="Current admin")
def current_admin(admin: AdminResponse = Depends(get_current_admin)):
    return ApiResponse(success=True, message="Current admin fetched successfully.", data=admin)


@router.post("/auth/me/phone/otp", response_model=ApiResponse[bool], summary="Send a phone verification code")
def request_phone_code(
    body: PhoneCodeRequest,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    service.request_phone_code(admin.id, body.phone_code, body.phone_number, admin)
    return ApiResponse(success=True, message="A code was sent to that phone number.", data=True)


@router.post("/auth/me/phone/verify", response_model=ApiResponse[AdminResponse], summary="Confirm a phone number")
def verify_phone_code(
    body: PhoneVerifyRequest,
    admin: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    updated = service.confirm_phone_code(admin.id, body.phone_code, body.phone_number, body.code, admin)
    return ApiResponse(success=True, message="Phone number verified.", data=updated)


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
        message="Admin created. The temporary password was emailed. The account activates after they verify their email and set a password.",
        data=service.create_admin(request, current),
    )


@router.post(
    "/admins/{admin_id}/email-code",
    response_model=ApiResponse[bool],
    summary="Email a code to confirm an address",
)
def email_code(
    admin_id: int,
    request: EmailCodeRequest,
    current: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
):
    service.request_email_code(admin_id, request.email, current)
    return ApiResponse(success=True, message="A code was sent to that email address.", data=True)


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


@router.put("/admins/{admin_id}/avatar", response_model=ApiResponse[AdminResponse], summary="Set admin profile picture")
def upload_admin_avatar(
    admin_id: int,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    current: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
    media: MediaService = Depends(get_admin_media),
):
    _can_edit_admin(admin_id, current)
    media.apply_admin_picture(admin_id, file, url)
    return ApiResponse(success=True, message="Profile picture updated successfully.", data=service.get_admin(admin_id))


@router.delete("/admins/{admin_id}/avatar", response_model=ApiResponse[AdminResponse], summary="Remove admin profile picture")
def remove_admin_avatar(
    admin_id: int,
    current: AdminResponse = Depends(get_current_admin),
    service: AuthService = Depends(get_service),
    media: MediaService = Depends(get_admin_media),
):
    _can_edit_admin(admin_id, current)
    media.remove_admin_avatar(admin_id)
    return ApiResponse(success=True, message="Profile picture removed successfully.", data=service.get_admin(admin_id))


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
