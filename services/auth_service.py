import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from core.permissions import (
    ALL_CODES,
    MANAGE_PERMISSIONS,
    UPDATE_ADMINS,
    USE_ASSISTANT,
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
    MfaStatus,
    MfaVerifyRequest,
    PasskeyOptionsResponse,
    PasskeyRegisterRequest,
    PasskeyResponse,
    ResetPasswordRequest,
    TokenResponse,
    TotpSetupResponse,
)
from core import totp
from core.config import settings
from core.secret_box import seal, unseal
from services import passkeys
from services.admin_codes import MAX_PER_HOUR, MemoryCodeStore, _utcnow
from services.admin_mfa import MAX_CHALLENGE_ATTEMPTS, MemoryMfaStore
from services.admin_sessions import MemorySessionStore
from services.mailer import MailError, MemoryMailer

TOTP_ISSUER = "Carrom Premier League"


class AuthService:
    def __init__(self, repository: AdminRepository, permissions: PermissionRepository, mailer=None, codes=None, sessions=None, mfa=None):
        self.repository = repository
        self.permissions = permissions
        self.mailer = mailer if mailer is not None else MemoryMailer()
        self.codes = codes if codes is not None else MemoryCodeStore()
        self.sessions = sessions if sessions is not None else MemorySessionStore()
        self.mfa = mfa if mfa is not None else MemoryMfaStore()

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

    def _issue_token(self, record: dict, expires_at=None) -> str:
        assistant_access = USE_ASSISTANT in self.permissions.codes_for(record["Id"])
        return create_access_token(
            record["Id"], record["Username"], record["TokenVersion"], assistant_access, expires_at
        )

    def refresh(self, admin: AdminResponse, token: str) -> TokenResponse:
        """Reissue the bearer token with the claims the database holds now. The expiry does not move."""
        payload = decode_access_token(token)
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
        fresh = create_access_token(
            admin.id,
            admin.username,
            int(payload["ver"]),
            USE_ASSISTANT in admin.permissions,
            expires_at,
        )
        remaining = max(0, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
        return TokenResponse(access_token=fresh, expires_in=remaining, must_change_password=admin.must_change_password)

    def login(self, request: LoginRequest) -> TokenResponse:
        record = self._matching_login(request.username, request.password)
        must_change = bool(record.get("MustChangePassword"))
        if not record["IsActive"] and not must_change:
            raise self._invalid_login()
        methods = [] if must_change else self._mfa_methods(record["Id"], record.get("Email"))
        if methods:
            challenge = self.mfa.create_challenge(record["Id"], "login", request.device_id, request.device_label)
            return TokenResponse(mfa_required=True, mfa_token=challenge.id, mfa_methods=methods)
        return self._start_session(record, request.device_id, request.device_label, must_change)

    def _start_session(self, record: dict, device_id: str | None, device_label: str | None, must_change: bool) -> TokenResponse:
        now = _utcnow()
        if device_id:
            waiting = self.sessions.begin(record["Id"], device_id, device_label, now)
            if waiting is not None:
                return TokenResponse(
                    access_token="",
                    expires_in=0,
                    must_change_password=must_change,
                    pending=True,
                    request_id=waiting.id,
                )
        token = self._issue_token(record)
        if device_id:
            self.sessions.activate(
                record["Id"],
                device_id,
                device_label,
                now + timedelta(minutes=settings.AUTH_TOKEN_EXPIRE_MINUTES),
            )
        return TokenResponse(
            access_token=token,
            expires_in=settings.AUTH_TOKEN_EXPIRE_MINUTES * 60,
            must_change_password=must_change,
        )

    def _mfa_methods(self, admin_id: int, email: str | None) -> list[str]:
        current = self.mfa.settings(admin_id)
        methods = []
        if self.mfa.passkeys(admin_id):
            methods.append("passkey")
        if current.totp_secret:
            methods.append("totp")
        if current.email and email:
            methods.append("email")
        return methods

    def _login_challenge(self, mfa_token: str):
        challenge = self.mfa.open_challenge(mfa_token, "login")
        if challenge is None:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="That sign-in expired. Sign in again.")
        admin = self.repository.get_by_id(challenge.admin_id)
        record = self.repository.get_credentials_by_username(admin.username) if admin else None
        if not record or not record["IsActive"]:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="That sign-in expired. Sign in again.")
        return challenge, admin, record

    def send_login_code(self, mfa_token: str) -> None:
        challenge, admin, record = self._login_challenge(mfa_token)
        if "email" not in self._mfa_methods(admin.id, admin.email):
            raise HTTPException(status_code=400, detail="Email codes are not turned on for this account.")
        self._issue_code(admin, "login", admin.email)

    def passkey_login_options(self, mfa_token: str, origin: str | None) -> dict:
        challenge, admin, _ = self._login_challenge(mfa_token)
        keys = self.mfa.passkeys(admin.id)
        if not keys:
            raise HTTPException(status_code=400, detail="This account has no passkey.")
        options, value = passkeys.authentication_options(origin, [key.credential_id for key in keys])
        self.mfa.set_challenge_value(challenge.id, value)
        return options

    def verify_mfa(self, request: MfaVerifyRequest, origin: str | None) -> TokenResponse:
        challenge, admin, record = self._login_challenge(request.mfa_token)
        if request.method not in self._mfa_methods(admin.id, admin.email):
            raise HTTPException(status_code=400, detail="That sign-in method is not turned on for this account.")
        if not self._second_factor_passes(request, challenge, admin, origin):
            attempts = self.mfa.count_failure(challenge.id)
            if attempts >= MAX_CHALLENGE_ATTEMPTS:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="Too many tries. Sign in again.")
            detail = "That passkey was not accepted." if request.method == "passkey" else "That code is not right."
            raise HTTPException(status_code=400, detail=detail)
        self.mfa.consume(challenge.id)
        return self._start_session(record, challenge.device_id, challenge.device_label, False)

    def _second_factor_passes(self, request: MfaVerifyRequest, challenge, admin: AdminResponse, origin: str | None) -> bool:
        if request.method == "email":
            return self.codes.verify(admin.id, "login", admin.email, request.code or "")
        if request.method == "totp":
            current = self.mfa.settings(admin.id)
            step = totp.match(unseal(current.totp_secret), request.code or "", last_step=current.totp_last_step)
            if step is None:
                return False
            self.mfa.update_settings(admin.id, totp_last_step=step)
            return True
        credential = request.credential or {}
        stored = self.mfa.passkey_by_credential(str(credential.get("id") or credential.get("rawId") or ""))
        if stored is None or stored.admin_id != admin.id or not challenge.challenge:
            return False
        try:
            sign_count = passkeys.verify_authentication(origin, credential, challenge.challenge, stored.public_key, stored.sign_count)
        except HTTPException:
            return False
        self.mfa.touch_passkey(stored.id, sign_count)
        return True

    def mfa_status(self, admin: AdminResponse) -> MfaStatus:
        current = self.mfa.settings(admin.id)
        return MfaStatus(
            email_enabled=bool(current.email and admin.email),
            email_available=bool(admin.email),
            totp_enabled=bool(current.totp_secret),
            passkeys=[
                PasskeyResponse(id=key.id, label=key.label, created_at=key.created_at, last_used_at=key.last_used_at)
                for key in self.mfa.passkeys(admin.id)
            ],
        )

    def set_email_factor(self, admin: AdminResponse, enabled: bool) -> MfaStatus:
        if enabled and not admin.email:
            raise HTTPException(status_code=400, detail="Add an email address to your profile first.")
        self.mfa.update_settings(admin.id, email=enabled)
        return self.mfa_status(admin)

    def start_totp(self, admin: AdminResponse) -> TotpSetupResponse:
        secret = totp.new_secret()
        self.mfa.update_settings(admin.id, totp_pending=seal(secret))
        account = admin.email or admin.username
        return TotpSetupResponse(secret=secret, otpauth_uri=totp.provisioning_uri(secret, account, TOTP_ISSUER))

    def confirm_totp(self, admin: AdminResponse, code: str) -> MfaStatus:
        current = self.mfa.settings(admin.id)
        if not current.totp_pending:
            raise HTTPException(status_code=400, detail="Start the authenticator setup again.")
        step = totp.match(unseal(current.totp_pending), code)
        if step is None:
            raise HTTPException(status_code=400, detail="That code is not right. Check the time on your phone and try again.")
        self.mfa.update_settings(admin.id, totp_secret=current.totp_pending, totp_pending=None, totp_last_step=step)
        return self.mfa_status(admin)

    def remove_totp(self, admin: AdminResponse) -> MfaStatus:
        self.mfa.update_settings(admin.id, totp_secret=None, totp_pending=None, totp_last_step=None)
        return self.mfa_status(admin)

    def passkey_registration_options(self, admin: AdminResponse, origin: str | None) -> PasskeyOptionsResponse:
        existing = [key.credential_id for key in self.mfa.passkeys(admin.id)]
        options, value = passkeys.registration_options(origin, admin, existing)
        challenge = self.mfa.create_challenge(admin.id, "passkey")
        self.mfa.set_challenge_value(challenge.id, value)
        return PasskeyOptionsResponse(challenge_id=challenge.id, options=options)

    def register_passkey(self, admin: AdminResponse, request: PasskeyRegisterRequest, origin: str | None) -> MfaStatus:
        challenge = self.mfa.open_challenge(request.challenge_id, "passkey")
        if challenge is None or challenge.admin_id != admin.id or not challenge.challenge:
            raise HTTPException(status_code=400, detail="That passkey request expired. Try again.")
        self.mfa.consume(challenge.id)
        credential_id, public_key, sign_count, transports = passkeys.verify_registration(
            origin, request.credential, challenge.challenge
        )
        if self.mfa.passkey_by_credential(credential_id):
            raise HTTPException(status_code=409, detail="That passkey is already saved.")
        self.mfa.add_passkey(admin.id, credential_id, public_key, sign_count, request.label, transports)
        return self.mfa_status(admin)

    def remove_passkey(self, admin: AdminResponse, passkey_id: int) -> MfaStatus:
        if not self.mfa.remove_passkey(admin.id, passkey_id):
            raise HTTPException(status_code=404, detail="Passkey not found.")
        return self.mfa_status(admin)

    def request_password_code(self, admin: AdminResponse) -> None:
        if not admin.email:
            raise HTTPException(status_code=400, detail="Add an email address to your profile first.")
        self._issue_code(admin, self._password_purpose(admin), admin.email)

    @staticmethod
    def _password_purpose(admin: AdminResponse) -> str:
        return "activation" if admin.must_change_password else "password_change"

    def pending_login(self, admin: AdminResponse):
        return self.sessions.pending_for(admin.id, _utcnow())

    def approve_login(self, admin: AdminResponse, request_id: str) -> None:
        now = _utcnow()
        challenge = self.sessions.require_pending(admin.id, request_id, now)
        self.repository.update(admin.id, {"increment_token_version": True})
        record = self.repository.get_credentials_by_username(admin.username)
        token = self._issue_token(record)
        expires = now + timedelta(minutes=settings.AUTH_TOKEN_EXPIRE_MINUTES)
        self.sessions.activate(admin.id, challenge.device_id, challenge.device_label, expires)
        self.sessions.resolve(admin.id, request_id, "approved", now, token)

    def deny_login(self, admin: AdminResponse, request_id: str) -> None:
        self.sessions.resolve(admin.id, request_id, "denied", _utcnow())

    def claim_login(self, request_id: str) -> TokenResponse:
        challenge = self.sessions.claim(request_id, _utcnow())
        if challenge is None or challenge.status in {"expired", "claimed"}:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="That sign-in request expired. Try again.")
        if challenge.status == "pending":
            return TokenResponse(access_token="", expires_in=0, pending=True, request_id=request_id)
        if challenge.status == "denied":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The other session declined this sign-in.")
        if challenge.status != "approved" or not challenge.access_token:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="That sign-in request expired. Try again.")
        return TokenResponse(
            access_token=challenge.access_token,
            expires_in=settings.AUTH_TOKEN_EXPIRE_MINUTES * 60,
        )

    def logout(self, admin: AdminResponse, device_id: str | None = None) -> None:
        if device_id:
            live = self.sessions.live(admin.id, _utcnow())
            if live is not None and live.device_id != device_id:
                return
        self.sessions.clear(admin.id)

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
        password_code = changes.pop("password_code", None)
        is_self = existing.id == current_admin.id
        if not is_self:
            if set(changes) - {"is_active"}:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admins can change only their own profile and password.",
                )
            if UPDATE_ADMINS not in current_admin.permissions:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=denied_message([UPDATE_ADMINS]))
            if existing.must_change_password:
                raise HTTPException(
                    status_code=400,
                    detail="This account activates itself after that admin verifies their email and sets a password.",
                )
        else:
            if "is_active" in changes:
                raise HTTPException(status_code=400, detail="You cannot switch your own account on or off.")
            if existing.must_change_password and set(changes) != {"password"}:
                raise HTTPException(
                    status_code=400,
                    detail="Verify your email and set your own password to activate this account.",
                )
        if "password" in changes:
            if not existing.email:
                raise HTTPException(status_code=400, detail="Add an email address to your profile first.")
            if not self.codes.verify(admin_id, self._password_purpose(existing), existing.email, password_code or ""):
                raise HTTPException(status_code=400, detail="Enter the code sent to your email address.")
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
        if changes.get("is_active") is False and not is_self:
            if self.repository.count_active() <= 1:
                raise HTTPException(status_code=400, detail="At least one active admin is required.")
            self._ensure_other_manager(existing)
            changes["increment_token_version"] = True
        ends_session = bool(changes.get("increment_token_version"))
        updated = self.repository.update(admin_id, changes)
        if ends_session:
            self.sessions.clear(admin_id)
        if password_changed and updated and updated.email:
            self._send(lambda: self.mailer.password_changed(updated, activated), None)
        return self.get_admin(admin_id)

    def request_email_code(self, admin_id: int, email: str, current_admin: AdminResponse) -> None:
        existing = self.repository.get_by_id(admin_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Admin user not found.")
        if existing.id != current_admin.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admins can change only their own profile and password.",
            )
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
        self.sessions.clear(record["Id"])
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
