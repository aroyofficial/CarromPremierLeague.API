"""Public shapes for device subscriptions and calendar feeds."""

from pydantic import BaseModel, Field


class PushSubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2000)
    p256dh: str = Field(min_length=1, max_length=255)
    auth: str = Field(min_length=1, max_length=255)
    season_id: int | None = None
    team_id: int | None = None
    notify_scheduled: bool = True
    notify_changed: bool = True
    notify_cancelled: bool = True
    notify_live: bool = True
    notify_ended: bool = True
    notify_score: bool = True
    notify_reminder: bool = True
    # Set by the service worker when the browser rotates a subscription. The new endpoint
    # keeps the old one's choices, and the old row is turned off.
    replaces_endpoint: str | None = Field(default=None, min_length=12, max_length=2000)


class PushPreferenceRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2000)
    season_id: int | None = None
    team_id: int | None = None
    notify_scheduled: bool = True
    notify_changed: bool = True
    notify_cancelled: bool = True
    notify_live: bool = True
    notify_ended: bool = True
    notify_score: bool = True
    notify_reminder: bool = True


class PushUnsubscribeRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2000)


class CalendarFeedRequest(BaseModel):
    season_id: int
    team_id: int | None = None
