from fastapi import HTTPException, status

from core.permissions import (
    ALL_CODES,
    MANAGE_PERMISSIONS,
    UPDATE_ADMINS,
    describe,
    denied_message,
)
from core.security import PASSWORD_ITERATIONS, create_access_token, decode_access_token, hash_password, verify_password
from repositories.admin_repository import AdminRepository
from repositories.permission_repository import PermissionRepository
from schemas.auth_schema import (
    AdminCreateRequest,
    AdminPermissionsUpdate,
    AdminResponse,
    AdminUpdateRequest,
    LoginRequest,
    TokenResponse,
)
from core.config import settings


class AuthService:
    def __init__(self, repository: AdminRepository, permissions: PermissionRepository):
        self.repository = repository
        self.permissions = permissions

    @staticmethod
    def _normalize_username(username: str) -> str:
        return username.strip().lower()

    @staticmethod
    def _known_codes(codes: list[str]) -> set[str]:
        unknown = sorted({code for code in codes if code not in ALL_CODES})
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(unknown)}.")
        return set(codes)

    def _with_permissions(self, admin: AdminResponse) -> AdminResponse:
        return admin.model_copy(update={"permissions": self.permissions.codes_for(admin.id)})

    def _ensure_other_manager(self, admin: AdminResponse) -> None:
        holders = self.permissions.active_holder_ids(MANAGE_PERMISSIONS, lock=True)
        if admin.is_active and admin.id in holders and len(holders) <= 1:
            raise HTTPException(
                status_code=400,
                detail=f"{admin.username} is the only active admin who can manage permissions. "
                "Give another active admin that permission first.",
            )

    def login(self, request: LoginRequest) -> TokenResponse:
        username = self._normalize_username(request.username)
        record = self.repository.get_credentials_by_username(username)
        if not record or not record["IsActive"] or not verify_password(
            request.password,
            record["PasswordHash"],
            record["PasswordSalt"],
            record["PasswordIterations"],
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return TokenResponse(
            access_token=create_access_token(record["Id"], record["Username"], record["TokenVersion"]),
            expires_in=settings.AUTH_TOKEN_EXPIRE_MINUTES * 60,
        )

    def current_admin(self, token: str) -> AdminResponse:
        payload = decode_access_token(token)
        try:
            admin_id = int(payload["sub"])
            token_version = int(payload["ver"])
        except (KeyError, TypeError, ValueError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        record = self.repository.get_credentials_by_username(payload.get("username", ""))
        if (
            not record
            or record["Id"] != admin_id
            or not record["IsActive"]
            or record["TokenVersion"] != token_version
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return AdminResponse(
            id=record["Id"],
            username=record["Username"],
            is_active=True,
            permissions=self.permissions.codes_for(record["Id"]),
        )

    def list_admins(self) -> list[AdminResponse]:
        grouped = self.permissions.codes_by_admin()
        return [
            admin.model_copy(update={"permissions": grouped.get(admin.id, [])})
            for admin in self.repository.get_all()
        ]

    def get_admin(self, admin_id: int) -> AdminResponse:
        admin = self.repository.get_by_id(admin_id)
        if not admin:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        return self._with_permissions(admin)

    def _create(self, request: AdminCreateRequest, codes: set[str], granted_by: int | None) -> AdminResponse:
        username = self._normalize_username(request.username)
        if self.repository.get_credentials_by_username(username):
            raise HTTPException(status_code=409, detail="Username already exists.")
        password_hash, password_salt = hash_password(request.password)
        created = self.repository.create(username, password_hash, password_salt, PASSWORD_ITERATIONS)
        if codes:
            self.permissions.change(created.id, codes, set(), granted_by)
        return self.get_admin(created.id)

    def create_admin(self, request: AdminCreateRequest, actor: AdminResponse) -> AdminResponse:
        requested = self._known_codes(request.permissions)
        if requested:
            if MANAGE_PERMISSIONS not in actor.permissions:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=denied_message([MANAGE_PERMISSIONS]),
                )
            missing = requested - set(actor.permissions)
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"You can only grant permissions you hold. You do not hold {describe(missing)}.",
                )
        return self._create(request, requested, actor.id)

    def bootstrap_admin(self, request: AdminCreateRequest) -> AdminResponse:
        if self.repository.count_all(lock=True) != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Admin bootstrap is no longer available.",
            )
        return self._create(request, set(ALL_CODES), None)

    def update_admin(
        self,
        admin_id: int,
        request: AdminUpdateRequest,
        current_admin: AdminResponse,
    ) -> AdminResponse:
        existing = self.repository.get_by_id(admin_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        changes = request.model_dump(exclude_unset=True)
        is_self = existing.id == current_admin.id
        if UPDATE_ADMINS not in current_admin.permissions and (not is_self or "is_active" in changes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denied_message([UPDATE_ADMINS]))
        if "username" in changes:
            username = self._normalize_username(changes["username"])
            duplicate = self.repository.get_credentials_by_username(username)
            if duplicate and duplicate["Id"] != admin_id:
                raise HTTPException(status_code=409, detail="Username already exists.")
            changes["username"] = username
        if "password" in changes:
            password_hash, password_salt = hash_password(changes.pop("password"))
            changes.update({
                "password_hash": password_hash,
                "password_salt": password_salt,
                "password_iterations": PASSWORD_ITERATIONS,
                "increment_token_version": True,
            })
        if changes.get("is_active") is False:
            if is_self:
                raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
            if self.repository.count_active() <= 1:
                raise HTTPException(status_code=400, detail="At least one active admin is required.")
            self._ensure_other_manager(existing)
            changes["increment_token_version"] = True
        self.repository.update(admin_id, changes)
        return self.get_admin(admin_id)

    def delete_admin(self, admin_id: int, current_admin: AdminResponse) -> bool:
        existing = self.repository.get_by_id(admin_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        if existing.id == current_admin.id:
            raise HTTPException(status_code=400, detail="You cannot delete your own account.")
        if existing.is_active and self.repository.count_active() <= 1:
            raise HTTPException(status_code=400, detail="At least one active admin is required.")
        self._ensure_other_manager(existing)
        return self.repository.delete(admin_id)

    def set_permissions(
        self,
        admin_id: int,
        request: AdminPermissionsUpdate,
        actor: AdminResponse,
    ) -> AdminResponse:
        existing = self.repository.get_by_id(admin_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        requested = self._known_codes(request.permissions)
        current = set(self.permissions.codes_for(admin_id))
        grant = requested - current
        revoke = current - requested
        outside = (grant | revoke) - set(actor.permissions)
        if outside:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"You can only change permissions you hold. You do not hold {describe(outside)}.",
            )
        if MANAGE_PERMISSIONS in revoke:
            if admin_id == actor.id:
                raise HTTPException(
                    status_code=400,
                    detail="You cannot remove your own 'Manage permissions' permission.",
                )
            self._ensure_other_manager(existing)
        if grant or revoke:
            self.permissions.change(admin_id, grant, revoke, actor.id)
        return self.get_admin(admin_id)
