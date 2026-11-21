from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

WidgetType = Literal["scorecard", "toss", "fixtures"]
WidgetStatus = Literal["active", "expired", "revoked"]


class WidgetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    widget_type: WidgetType
    match_id: Optional[int] = Field(default=None, gt=0)
    season_id: Optional[int] = Field(default=None, gt=0)
    expires_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Enter a name for this widget.")
        return cleaned

    @model_validator(mode="after")
    def _check_target(self):
        if self.widget_type == "fixtures":
            if self.season_id is None or self.match_id is not None:
                raise ValueError("A fixtures widget shows one season, so it needs a season and no match.")
        elif self.match_id is None or self.season_id is not None:
            raise ValueError("A scorecard or toss widget shows one match, so it needs a match and no season.")
        return self


class WidgetUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    is_active: Optional[bool] = None
    expires_at: Optional[datetime] = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Enter a name for this widget.")
        return cleaned


class WidgetResponse(BaseModel):
    id: int
    name: str
    widget_type: WidgetType
    match_id: Optional[int] = None
    season_id: Optional[int] = None
    token_prefix: str
    is_active: bool
    status: WidgetStatus
    expires_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    created_by: int


class WidgetCreatedResponse(WidgetResponse):
    token: str


class WidgetListResponse(BaseModel):
    widgets: list[WidgetResponse]
