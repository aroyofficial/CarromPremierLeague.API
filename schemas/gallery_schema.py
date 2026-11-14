from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field


class GalleryPhoto(BaseModel):
    id: int
    source_type: str
    url: str
    thumbnail_url: str
    placeholder_url: Optional[str] = None
    width: int
    height: int
    caption: Optional[str] = None
    alt_text: Optional[str] = None
    taken_on: Optional[date] = None
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
    taken_on: Optional[date] = None
    is_published: Optional[bool] = None
    season_ids: Optional[list[int]] = None
    clear_taken_on: bool = False


class GalleryOrderRequest(BaseModel):
    season_id: int
    item_ids: list[int] = Field(default_factory=list)


class GalleryCoverRequest(BaseModel):
    item_id: int


class GalleryBulkRequest(BaseModel):
    action: Literal["publish", "hide", "add_seasons", "delete"]
    ids: list[int] = Field(default_factory=list)
    season_ids: list[int] = Field(default_factory=list)


class GalleryBulkResult(BaseModel):
    updated: int
