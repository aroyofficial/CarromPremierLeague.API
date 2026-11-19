from datetime import date, datetime, time
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, Field


def parse_taken_on(value):
    """A gallery time is the clock time picked in the admin, with no zone conversion."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None, microsecond=0)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if len(text) == 10:
            return datetime.combine(date.fromisoformat(text), time.min)
        return datetime.fromisoformat(text).replace(tzinfo=None, microsecond=0)
    raise ValueError("Taken on must be a date or a date and time.")


TakenOn = Annotated[Optional[datetime], BeforeValidator(parse_taken_on)]


class GalleryPhoto(BaseModel):
    id: int
    source_type: str
    media_type: Literal["image", "video"] = "image"
    url: str
    thumbnail_url: str
    placeholder_url: Optional[str] = None
    width: int
    height: int
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    taken_on: TakenOn = None
    sort_order: int = 0
    is_cover: bool = False
    is_published: bool = False
    season_ids: list[int] = Field(default_factory=list)


class GalleryPage(BaseModel):
    items: list[GalleryPhoto]
    next_cursor: Optional[str] = None


class GallerySeason(BaseModel):
    id: int
    name: str
    photo_count: int
    cover: Optional[GalleryPhoto] = None


class GalleryUpdateRequest(BaseModel):
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    taken_on: TakenOn = None
    is_published: Optional[bool] = None
    season_ids: Optional[list[int]] = None
    clear_taken_on: bool = False


class GalleryOrderRequest(BaseModel):
    season_id: Optional[int] = None
    item_ids: list[int] = Field(default_factory=list)


class GalleryCoverRequest(BaseModel):
    item_id: int


class GalleryBulkRequest(BaseModel):
    action: Literal["publish", "hide", "add_seasons", "delete"]
    ids: list[int] = Field(default_factory=list)
    season_ids: list[int] = Field(default_factory=list)


class GalleryBulkResult(BaseModel):
    updated: int
