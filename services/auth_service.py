import secrets
from datetime import timedelta

from fastapi import HTTPException, status

from core.permissions import (
    ALL_CODES,
    MANAGE_PERMISSIONS,
    UPDATE_ADMINS,
    describe,
    denied_message,
    missing_parent_message,
    missing_parents,
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
    ResetPasswordRequest,
    TokenResponse,
)
from core.config import settings
from services.admin_codes import MAX_PER_HOUR, MemoryCodeStore, _utcnow
from services.mailer import MailError, MemoryMailer


class AuthService:
    def __init__(self, repository: AdminRepository, permissions: PermissionRepository, mailer=None, codes=None):
        self.repository = repository
        self.permissions = permissions
        self.mailer = mailer if mailer is not None else MemoryMailer()
        self.codes = codes if codes is not None else MemoryCodeStore()

    @staticmethod
    def _normalize_username(username: str) -> str:
        return username.strip().lower()

    @staticmethod
    def _known_codes(codes: list[str]) -> set[str]:
        unknown = sorted({code for code in codes if code not in ALL_CODES})
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(unknown)}.")
        return set(codes)

    @staticmethod
    def _ensure_parents(codes: set[str]) -> None:
        missing = missing_parents(codes)
        if missing:
            raise HTTPException(status_code=400, detail=missing_parent_message(missing))

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
        record = self._matching_login(request.username, request.password)
        must_change = bool(record.get("MustChangePassword"))
        if not record["IsActive"] and not must_change:
            raise self._invalid_login()
        return TokenResponse(
            access_token=create_access_token(record["Id"], record["Username"], record["TokenVersion"]),
            expires_in=settings.AUTH_TOKEN_EXPIRE_MINUTES * 60,
            must_change_password=must_change,
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
        must_change = bool(record.get("MustChangePassword")) if record else False
        if (
            not record
            or record["Id"] != admin_id
            or record["TokenVersion"] != token_version
            or (not record["IsActive"] and not must_change)
        ):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired bearer token.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return AdminResponse(
            id=record["Id"],
            username=record["Username"],
            is_active=bool(record["IsActive"]),
            must_change_password=must_change,
            permissions=self.permissions.codes_for(record["Id"]),
            first_name=record.get("FirstName"),
            last_name=record.get("LastName"),
            date_of_birth=AdminRepository._as_date(record.get("DateOfBirth")),
            gender=record.get("Gender"),
            phone_code=record.get("PhoneCode"),
            phone_number=record.get("PhoneNumber"),
            email=record.get("Email"),
            avatar_url=record.get("AvatarUrl"),
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

    def _create(
        self,
        request: AdminCreateRequest,
        codes: set[str],
        granted_by: int | None,
        *,
        password: str,
        is_active: bool,
        must_change_password: bool,
    ) -> AdminResponse:
        username = self._normalize_username(request.username)
        if self.repository.get_credentials_by_username(username):
            raise HTTPException(status_code=409, detail="Username already exists.")
        password_hash, password_salt = hash_password(password)
        created = self.repository.create(
            username,
            password_hash,
            password_salt,
            PASSWORD_ITERATIONS,
            profile=self._profile(request),
            is_active=is_active,
            must_change_password=must_change_password,
        )
        if codes:
            self.permissions.change(created.id, codes, set(), granted_by)
        return self.get_admin(created.id)

    @staticmethod
    def _profile(request: AdminCreateRequest) -> dict:
        return {
            "first_name": request.first_name,
            "last_name": request.last_name,
            "date_of_birth": request.date_of_birth,
            "gender": request.gender,
            "phone_code": request.phone_code,
            "phone_number": request.phone_number,
            "email": request.email,
        }

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
            self._ensure_parents(requested)
        self._require_identity(request)
        self._require_mail()
        password = self._generated_password()
        created = self._create(
            request,
            requested,
            actor.id,
            password=password,
            is_active=False,
            must_change_password=True,
        )
        self._send(lambda: self.mailer.welcome(created, password), created.id)
        return created

    def bootstrap_admin(self, request: AdminCreateRequest) -> AdminResponse:
        if self.repository.count_all(lock=True) != 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Admin bootstrap is no longer available.",
            )
        if not request.password:
            raise HTTPException(status_code=400, detail="Choose a password for the first admin.")
        return self._create(
            request,
            set(ALL_CODES),
            None,
            password=request.password,
            is_active=True,
            must_change_password=False,
        )

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
        email_code = changes.pop("email_code", None)
        is_self = existing.id == current_admin.id
        if existing.must_change_password and is_self and set(changes) != {"password"}:
            raise HTTPException(
                status_code=400,
                detail="Change the temporary password to activate this account.",
            )
        if UPDATE_ADMINS not in current_admin.permissions and (not is_self or "is_active" in changes):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denied_message([UPDATE_ADMINS]))
        if "email" in changes:
            if not changes["email"]:
                raise HTTPException(status_code=400, detail="Email is required.")
            if changes["email"] != existing.email:
                owner = self.repository.find_email(changes["email"])
                if owner and owner != admin_id:
                    raise HTTPException(status_code=409, detail="That email address is already used by an admin.")
                if not self.codes.verify(admin_id, "email_change", changes["email"], email_code or ""):
                    raise HTTPException(status_code=400, detail="Enter the code sent to that email address.")
        if "phone_code" in changes or "phone_number" in changes:
            code = changes["phone_code"] if "phone_code" in changes else existing.phone_code
            number = changes["phone_number"] if "phone_number" in changes else existing.phone_number
            if not code or not number:
                raise HTTPException(status_code=400, detail="Contact number is required.")
        if "username" in changes:
            username = self._normalize_username(changes["username"])
            duplicate = self.repository.get_credentials_by_username(username)
            if duplicate and duplicate["Id"] != admin_id:
                raise HTTPException(status_code=409, detail="Username already exists.")
            changes["username"] = username
        password_changed = "password" in changes
        asked_to_activate = changes.get("is_active") is True
        activated = False
        if password_changed:
            password_hash, password_salt = hash_password(changes.pop("password"))
            changes.update({
                "password_hash": password_hash,
                "password_salt": password_salt,
                "password_iterations": PASSWORD_ITERATIONS,
                "increment_token_version": True,
            })
            if existing.must_change_password:
                changes["is_active"] = True
                changes["must_change_password"] = False
                activated = True
        if asked_to_activate and existing.must_change_password and not activated:
            raise HTTPException(
                status_code=400,
                detail="This account stays off until that admin changes the temporary password.",
            )
        if changes.get("is_active") is False:
            if is_self:
                raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
            if self.repository.count_active() <= 1:
                raise HTTPException(status_code=400, detail="At least one active admin is required.")
            self._ensure_other_manager(existing)
            changes["increment_token_version"] = True
        updated = self.repository.update(admin_id, changes)
        if password_changed and updated and updated.email:
            self._send(lambda: self.mailer.password_changed(updated, activated), None)
        return self.get_admin(admin_id)

    def request_email_code(self, admin_id: int, email: str, current_admin: AdminResponse) -> None:
        existing = self.repository.get_by_id(admin_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        if existing.id != current_admin.id and UPDATE_ADMINS not in current_admin.permissions:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denied_message([UPDATE_ADMINS]))
        if email == existing.email:
            raise HTTPException(status_code=400, detail="That is already the email address on this account.")
        owner = self.repository.find_email(email)
        if owner and owner != admin_id:
            raise HTTPException(status_code=409, detail="That email address is already used by an admin.")
        self._issue_code(existing, "email_change", email)

    def request_password_reset(self, username: str) -> None:
        record = self.repository.get_credentials_by_username(self._normalize_username(username))
        if not record or not record.get("Email"):
            return
        if not record["IsActive"] and not record.get("MustChangePassword"):
            return
        admin = self.repository.get_by_id(record["Id"])
        if admin:
            self._issue_code(admin, "password_reset", admin.email)

    def reset_password(self, request: ResetPasswordRequest) -> None:
        username = self._normalize_username(request.username)
        record = self.repository.get_credentials_by_username(username)
        if not record or not record.get("Email"):
            raise HTTPException(status_code=400, detail="That reset code is not valid.")
        if not record["IsActive"] and not record.get("MustChangePassword"):
            raise HTTPException(status_code=400, detail="That reset code is not valid.")
        if not self.codes.verify(record["Id"], "password_reset", record["Email"], request.code):
            raise HTTPException(status_code=400, detail="That reset code is not valid.")
        password_hash, password_salt = hash_password(request.password)
        changes = {
            "password_hash": password_hash,
            "password_salt": password_salt,
            "password_iterations": PASSWORD_ITERATIONS,
            "increment_token_version": True,
        }
        if record.get("MustChangePassword"):
            changes["is_active"] = True
            changes["must_change_password"] = False
        updated = self.repository.update(record["Id"], changes)
        if updated and updated.email:
            self._send(lambda: self.mailer.password_changed(updated, bool(record.get("MustChangePassword"))), None)

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
        self._ensure_parents(requested)
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

    def _matching_login(self, username: str, password: str) -> dict:
        record = self.repository.get_credentials_by_username(self._normalize_username(username))
        if not record or not verify_password(
            password,
            record["PasswordHash"],
            record["PasswordSalt"],
            record["PasswordIterations"],
        ):
            raise self._invalid_login()
        return record

    @staticmethod
    def _invalid_login() -> HTTPException:
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def _require_mail(self) -> None:
        if not getattr(self.mailer, "configured", False):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Email is not configured. Set the mail sender on the API before this can be sent.",
            )

    def _require_identity(self, request: AdminCreateRequest) -> None:
        if not request.email:
            raise HTTPException(status_code=400, detail="Email is required.")
        if not request.phone_code or not request.phone_number:
            raise HTTPException(status_code=400, detail="Contact number is required.")
        if self.repository.find_email(request.email):
            raise HTTPException(status_code=409, detail="That email address is already used by an admin.")

    @staticmethod
    def _generated_password() -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        return "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4))

    def _issue_code(self, admin: AdminResponse, purpose: str, destination: str) -> None:
        self._require_mail()
        since = _utcnow() - timedelta(hours=1)
        if self.codes.count_since(admin.id, purpose, since) >= MAX_PER_HOUR:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many codes were requested. Wait a while and try again.",
            )
        code = self.codes.issue(admin.id, purpose, destination)
        self._send(lambda: self.mailer.code(admin, purpose, code, destination), None)

    def _send(self, action, rollback_id: int | None) -> None:
        try:
            action()
        except MailError:
            if rollback_id is not None:
                self.repository.delete(rollback_id)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="The welcome email could not be sent, so the account was not created.",
                )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="The change was saved, but the email could not be sent.",
            )
