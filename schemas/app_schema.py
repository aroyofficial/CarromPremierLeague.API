from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


class AppScopeOption(BaseModel):
    code: str
    area: str
    area_name: str
    name: str
    description: str
    parent: Optional[str] = None


class AppCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=400)
    permissions: list[str] = Field(default_factory=list)
    expires_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("App name cannot be blank.")
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class AppUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=400)
    permissions: Optional[list[str]] = None
    is_active: Optional[bool] = None
    expires_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("App name cannot be blank.")
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_optional_description(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class AppResponse(BaseModel):
    id: int
    name: str
    description: Optional[str] = None
    key_prefix: str
    permissions: list[str] = Field(default_factory=list)
    is_active: bool = True
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: int


class AppCreatedResponse(AppResponse):
    api_key: str


class AppListResponse(BaseModel):
    apps: list[AppResponse]
    available_scopes: list[AppScopeOption]


class AppIdentityResponse(BaseModel):
    id: int
    name: str
    key_prefix: str
    permissions: list[str]
    expires_at: Optional[datetime] = None
