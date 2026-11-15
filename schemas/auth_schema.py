from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    password: str = Field(..., min_length=12, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    must_change_password: bool = False


class AdminProfileFields(BaseModel):
    first_name: str | None = Field(None, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    date_of_birth: date | None = None
    gender: Literal["female", "male", "other"] | None = None
    phone_code: str | None = Field(None, max_length=8, pattern=r"^\+[1-9]\d{0,3}$")
    phone_number: str | None = Field(None, max_length=20, pattern=r"^\d{6,15}$")
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

    @field_validator("date_of_birth")
    @classmethod
    def birth_date_not_in_the_future(cls, value):
        if value is not None and value > date.today():
            raise ValueError("Date of birth cannot be in the future.")
        return value

    @model_validator(mode="after")
    def contact_number_is_complete(self):
        if (self.phone_code is None) != (self.phone_number is None):
            raise ValueError("Contact number needs both a country code and a number.")
        return self


class AdminCreateRequest(AdminProfileFields):
    username: str = Field(..., min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str | None = Field(
        None,
        min_length=12,
        max_length=256,
        description="Ignored when an admin creates the account. The system generates a temporary password.",
    )
    permissions: list[str] = Field(default_factory=list, max_length=100)


class AdminUpdateRequest(AdminProfileFields):
    username: str | None = Field(None, min_length=3, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str | None = Field(None, min_length=12, max_length=256)
    is_active: bool | None = None
    email_code: str | None = Field(None, min_length=6, max_length=6, pattern=r"^\d{6}$")


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
    code: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")
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
