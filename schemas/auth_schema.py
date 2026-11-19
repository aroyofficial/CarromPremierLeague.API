from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=12, max_length=256)
    device_id: str | None = Field(None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    device_label: str | None = Field(None, max_length=160)


class TokenResponse(BaseModel):
    access_token: str = ""
    token_type: str = "bearer"
    expires_in: int = 0
    must_change_password: bool = False
    pending: bool = False
    request_id: str | None = None
    mfa_required: bool = False
    mfa_token: str | None = None
    mfa_methods: list[str] = Field(default_factory=list)


class LogoutRequest(BaseModel):
    device_id: str | None = Field(None, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class LoginApproval(BaseModel):
    request_id: str = Field(..., min_length=8, max_length=64)


class PendingLogin(BaseModel):
    request_id: str
    device_label: str
    expires_in: int = Field(..., ge=0)


class AdminProfileFields(BaseModel):
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    date_of_birth: date | None = None
    gender: Literal["female", "male", "other"] | None = None
    phone_code: str | None = Field(None, max_length=8, pattern=r"^\+[1-9]\d{0,3}$")
    phone_number: str | None = Field(None, max_length=20)
    email: str | None = Field(None, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    @field_validator("first_name", "last_name", "phone_code", "phone_number", "email", mode="before")
    @classmethod
    def blank_to_none(cls, value):
        if value is None or not isinstance(value, str):
            return value
        text = value.strip()
        return text or None

    @field_validator("gender", mode="before")
    @classmethod
    def normalize_gender(cls, value):
        if isinstance(value, str):
            text = value.strip().lower()
            return text or None
        return value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value):
        return value.lower() if isinstance(value, str) else value

    @field_validator("phone_number")
    @classmethod
    def keep_phone_text(cls, value):
        return value

    @field_validator("date_of_birth")
    @classmethod
    def birth_date_not_in_the_future(cls, value):
        if value is not None and value > date.today():
            raise ValueError("Date of birth cannot be in the future.")
        return value

    @model_validator(mode="after")
    def contact_number_is_complete(self):
        if self.phone_code is None and self.phone_number is None:
            return self
        if (self.phone_code is None) != (self.phone_number is None):
            raise ValueError("Contact number needs both a country code and a number.")
        from core.phone_numbers import PhoneNumberError, canonicalize

        try:
            code, national, _e164 = canonicalize(self.phone_code, self.phone_number)
        except PhoneNumberError as exc:
            raise ValueError(str(exc)) from exc
        self.phone_code = code
        self.phone_number = national
        return self


class AdminCreateRequest(AdminProfileFields):
    username: str | None = Field(
        None,
        min_length=3,
        max_length=100,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Ignored when a name or email is present. The username is generated from those and cannot be changed.",
    )
    password: str | None = Field(
        None,
        min_length=12,
        max_length=256,
        description="Ignored when an admin creates the account. The system generates a temporary password.",
    )
    permissions: list[str] = Field(default_factory=list, max_length=100)


class AdminUpdateRequest(AdminProfileFields):
    username: str | None = Field(
        None,
        min_length=3,
        max_length=100,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Rejected when it differs from the current username. A username cannot be changed.",
    )
    password: str | None = Field(None, min_length=12, max_length=256)
    is_active: bool | None = None
    email_code: str | None = Field(None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    password_code: str | None = Field(None, min_length=6, max_length=6, pattern=r"^\d{6}$")


class MfaTokenRequest(BaseModel):
    mfa_token: str = Field(..., min_length=16, max_length=64)


class MfaVerifyRequest(MfaTokenRequest):
    method: Literal["email", "totp", "passkey"]
    code: str | None = Field(None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    credential: dict | None = None


class PasskeyResponse(BaseModel):
    id: int
    label: str
    created_at: datetime
    last_used_at: datetime | None = None


class MfaStatus(BaseModel):
    email_enabled: bool
    email_available: bool
    totp_enabled: bool
    passkeys: list[PasskeyResponse]


class TotpSetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpConfirmRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class EmailFactorRequest(BaseModel):
    enabled: bool


class PasskeyOptionsResponse(BaseModel):
    challenge_id: str
    options: dict


class PasskeyRegisterRequest(BaseModel):
    challenge_id: str = Field(..., min_length=16, max_length=64)
    credential: dict
    label: str = Field(..., min_length=1, max_length=100)

    @field_validator("label", mode="before")
    @classmethod
    def trim_label(cls, value):
        return value.strip() if isinstance(value, str) else value


class EmailCodeRequest(BaseModel):
    email: str = Field(..., max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.strip().lower()


class ForgotPasswordRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)


class ResetPasswordRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    code: str | None = Field(None, min_length=6, max_length=6, pattern=r"^\d{6}$")
    ticket: str | None = Field(None, min_length=20, max_length=128)
    password: str = Field(..., min_length=12, max_length=256)

    @model_validator(mode="after")
    def needs_code_or_ticket(self):
        if not self.code and not self.ticket:
            raise ValueError("Send the reset code or the ticket from verifying it.")
        return self


class ResetCodeRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class PasswordCodeVerifyRequest(BaseModel):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class PasswordTicket(BaseModel):
    ticket: str
    expires_in: int = Field(..., ge=0)


class ChangePasswordRequest(BaseModel):
    ticket: str = Field(..., min_length=20, max_length=128)
    password: str = Field(..., min_length=12, max_length=256)


class AdminPermissionsUpdate(BaseModel):
    permissions: list[str] = Field(..., max_length=100)


class AdminResponse(BaseModel):
    id: int
    username: str
    is_active: bool
    permissions: list[str] = Field(default_factory=list)
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    phone_code: str | None = None
    phone_number: str | None = None
    email: str | None = None
    avatar_url: str | None = None
    must_change_password: bool = False
    password_changed_at: datetime | None = None
    phone_verified: bool = False
    phone_verification_enabled: bool = False


class PhoneCodeRequest(BaseModel):
    phone_code: str = Field(..., min_length=1, max_length=8)
    phone_number: str = Field(..., min_length=1, max_length=20)


class PhoneVerifyRequest(PhoneCodeRequest):
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


class PermissionResponse(BaseModel):
    code: str
    area: str
    name: str
    description: str
    area_code: str
    parent: str | None = None


class PermissionNode(BaseModel):
    code: str
    name: str
    description: str
    parent: str | None = None


class PermissionAreaNode(BaseModel):
    code: str
    name: str
    description: str
    icon: str
    permissions: list[PermissionNode]


class PermissionSectionNode(BaseModel):
    code: str
    name: str
    description: str
    areas: list[PermissionAreaNode]


class PermissionRoleResponse(BaseModel):
    code: str
    name: str
    description: str
    permissions: list[str]


class PermissionTreeResponse(BaseModel):
    sections: list[PermissionSectionNode]
    roles: list[PermissionRoleResponse]
