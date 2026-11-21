from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class WebhookEventOption(BaseModel):
    code: str
    group: str
    name: str
    description: str


class WebhookCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    url: str = Field(..., min_length=1, max_length=2000)
    description: Optional[str] = Field(default=None, max_length=400)
    events: list[str] = Field(..., min_length=1)
    is_active: bool = True
    auth_type: Optional[Literal["none", "bearer", "basic", "header"]] = None
    auth_token: Optional[str] = Field(default=None, max_length=500)
    auth_username: Optional[str] = Field(default=None, max_length=200)
    auth_password: Optional[str] = Field(default=None, max_length=500)
    auth_header_name: Optional[str] = Field(default=None, max_length=100)
    auth_header_value: Optional[str] = Field(default=None, max_length=500)
    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Webhook name cannot be blank.")
        return cleaned

    @field_validator("url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Webhook URL cannot be blank.")
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_description(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class WebhookUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    url: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    description: Optional[str] = Field(default=None, max_length=400)
    events: Optional[list[str]] = None
    is_active: Optional[bool] = None
    auth_type: Optional[Literal["none", "bearer", "basic", "header"]] = None
    auth_token: Optional[str] = Field(default=None, max_length=500)
    auth_username: Optional[str] = Field(default=None, max_length=200)
    auth_password: Optional[str] = Field(default=None, max_length=500)
    auth_header_name: Optional[str] = Field(default=None, max_length=100)
    auth_header_value: Optional[str] = Field(default=None, max_length=500)
    @field_validator("name")
    @classmethod
    def _strip_optional_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Webhook name cannot be blank.")
        return cleaned

    @field_validator("url")
    @classmethod
    def _strip_optional_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Webhook URL cannot be blank.")
        return cleaned

    @field_validator("description")
    @classmethod
    def _clean_optional_description(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class WebhookResponse(BaseModel):
    id: int
    name: str
    url: str
    description: Optional[str] = None
    secret_hint: str
    auth_type: str = "none"
    auth_header_name: Optional[str] = None
    auth_hint: Optional[str] = None
    events: list[str]
    is_active: bool = True
    failure_count: int = 0
    last_delivered_at: Optional[datetime] = None
    last_status_code: Optional[int] = None
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: int


class WebhookCreatedResponse(WebhookResponse):
    signing_secret: str


class WebhookListResponse(BaseModel):
    webhooks: list[WebhookResponse]
    available_events: list[WebhookEventOption]


class WebhookDeliveryResponse(BaseModel):
    id: int
    webhook_id: int
    event_id: str
    event_type: str
    status: str
    attempts: int
    response_status: Optional[int] = None
    duration_ms: Optional[int] = None
    error_detail: Optional[str] = None
    created_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None


class WebhookTestResponse(BaseModel):
    delivery_id: int
    event_id: str
    ok: bool
    status_code: Optional[int] = None
    duration_ms: int
    error: Optional[str] = None
