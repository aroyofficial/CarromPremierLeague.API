from fastapi import HTTPException, status

from core.security import PASSWORD_ITERATIONS, create_access_token, decode_access_token, hash_password, verify_password
from repositories.admin_repository import AdminRepository
from schemas.auth_schema import (
    AdminCreateRequest,
    AdminResponse,
    AdminUpdateRequest,
    LoginRequest,
    TokenResponse,
)
from core.config import settings


class AuthService:
    def __init__(self, repository: AdminRepository):
        self.repository = repository

    @staticmethod
    def _normalize_username(username: str) -> str:
        return username.strip().lower()

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
        return AdminResponse(id=record["Id"], username=record["Username"], is_active=True)

    def list_admins(self) -> list[AdminResponse]:
        return self.repository.get_all()

    def get_admin(self, admin_id: int) -> AdminResponse:
        admin = self.repository.get_by_id(admin_id)
        if not admin:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        return admin

    def create_admin(self, request: AdminCreateRequest) -> AdminResponse:
        username = self._normalize_username(request.username)
        if self.repository.get_credentials_by_username(username):
            raise HTTPException(status_code=409, detail="Username already exists.")
        password_hash, password_salt = hash_password(request.password)
        return self.repository.create(username, password_hash, password_salt, PASSWORD_ITERATIONS)

    def bootstrap_admin(self, request: AdminCreateRequest) -> AdminResponse:
        if self.repository.count_all(lock=True) != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Admin bootstrap is no longer available.",
            )
        return self.create_admin(request)

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
            if existing.id == current_admin.id:
                raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
            if self.repository.count_active() <= 1:
                raise HTTPException(status_code=400, detail="At least one active admin is required.")
            changes["increment_token_version"] = True
        return self.repository.update(admin_id, changes)

    def delete_admin(self, admin_id: int, current_admin: AdminResponse) -> bool:
        existing = self.repository.get_by_id(admin_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        if existing.id == current_admin.id:
            raise HTTPException(status_code=400, detail="You cannot delete your own account.")
        if existing.is_active and self.repository.count_active() <= 1:
            raise HTTPException(status_code=400, detail="At least one active admin is required.")
        return self.repository.delete(admin_id)
