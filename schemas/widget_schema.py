from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

WidgetType = Literal["season_flow", "scorecard", "toss", "fixtures", "points_table", "stats"]
WidgetStatus = Literal["active", "expired", "revoked"]
RotationKind = Literal["points_table", "stats", "bracket", "fixtures", "last_result", "next_match", "clean_pause"]
DirectorMode = Literal["auto", "clean", "pin"]
DirectorCard = Literal[
    "match_ready",
    "toss",
    "live",
    "match_result",
    "scorecard",
    "points_table",
    "stats",
    "bracket",
    "fixtures",
    "next_match",
]
IdleMode = Literal["loop", "once_then_clean"]
ThemeAccent = Literal["gold", "royal", "crimson"]
TransitionStyle = Literal["reveal", "flip"]

# The standings card stays up this long so every row can be read.
POINTS_TABLE_SECONDS = 30

SEASON_SCOPED_TYPES = frozenset({"season_flow", "fixtures", "points_table", "stats"})


class FlowMatchStages(BaseModel):
    show_ready: bool = True
    show_toss: bool = True
    show_live: bool = True
    show_clock: bool = True
    show_player_strip: bool = True
    result_duration_sec: int = Field(default=20, ge=0, le=180)


class FlowRotationStep(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    kind: RotationKind
    duration_sec: int = Field(default=15, ge=5, le=180)
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def _clean_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Each flow step needs an id.")
        return cleaned


class FlowDirectorState(BaseModel):
    mode: DirectorMode = "auto"
    card: Optional[DirectorCard] = None
    match_id: Optional[int] = Field(default=None, gt=0)
    until_utc: Optional[datetime] = None

    @model_validator(mode="after")
    def _validate_pin(self):
        if self.mode == "pin" and self.card is None:
            raise ValueError("Choose a broadcast card to pin on screen.")
        if self.mode != "pin":
            self.card = None
            self.match_id = None
        return self

    @field_serializer("until_utc")
    def _serialize_until(self, value: Optional[datetime]) -> Optional[str]:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def default_flow_rotation() -> list[FlowRotationStep]:
    return [
        FlowRotationStep(id="step-points", kind="points_table", duration_sec=POINTS_TABLE_SECONDS, enabled=True),
        FlowRotationStep(id="step-stats", kind="stats", duration_sec=15, enabled=True),
        FlowRotationStep(id="step-bracket", kind="bracket", duration_sec=20, enabled=True),
        FlowRotationStep(id="step-fixtures", kind="fixtures", duration_sec=15, enabled=True),
    ]


class WidgetFlowConfig(BaseModel):
    idle_mode: IdleMode = "loop"
    theme_accent: ThemeAccent = "gold"
    transition_style: TransitionStyle = "reveal"
    match_stages: FlowMatchStages = Field(default_factory=FlowMatchStages)
    rotation: list[FlowRotationStep] = Field(default_factory=default_flow_rotation, max_length=20)
    director: FlowDirectorState = Field(default_factory=FlowDirectorState)


class WidgetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    widget_type: WidgetType
    match_id: Optional[int] = Field(default=None, gt=0)
    season_id: Optional[int] = Field(default=None, gt=0)
    expires_at: Optional[datetime] = None
    flow_config: Optional[WidgetFlowConfig] = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Enter a name for this widget.")
        return cleaned

    @model_validator(mode="after")
    def _check_target(self):
        if self.widget_type in SEASON_SCOPED_TYPES:
            if self.season_id is None or self.match_id is not None:
                raise ValueError("This widget shows a whole season, so it needs a season and no match.")
        elif self.match_id is None or self.season_id is not None:
            raise ValueError("A scorecard or toss widget shows one match, so it needs a match and no season.")
        if self.widget_type == "season_flow" and self.flow_config is None:
            self.flow_config = WidgetFlowConfig()
        elif self.widget_type != "season_flow":
            self.flow_config = None
        return self


class WidgetUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    is_active: Optional[bool] = None
    expires_at: Optional[datetime] = None
    flow_config: Optional[WidgetFlowConfig] = None
    director: Optional[FlowDirectorState] = None

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
    flow_config: Optional[WidgetFlowConfig] = None


class WidgetCreatedResponse(WidgetResponse):
    token: str


class WidgetListResponse(BaseModel):
    widgets: list[WidgetResponse]
