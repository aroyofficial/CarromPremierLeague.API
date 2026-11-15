import logging
import re
import struct
from datetime import date, datetime
from urllib.parse import urlparse

from fastapi import HTTPException, UploadFile

from core.permissions import denied_message
from core.storage import StoredImage
from schemas.gallery_schema import (
    GalleryBulkRequest,
    GalleryBulkResult,
    GalleryCoverRequest,
    GalleryOrderRequest,
    GalleryPage,
    GalleryPhoto,
    GallerySeason,
    GalleryUpdateRequest,
)
from services.media_service import MAX_IMAGE_BYTES, image_format, normalize_picture_link

logger = logging.getLogger(__name__)

_MAX_EDGE = 20000
_MAX_SEASONS = 20
_MAX_BULK = 100
_MAX_VIDEO_BYTES = 50 * 1024 * 1024
_VIDEO_FRAME = (1920, 1080)
_YOUTUBE_ID = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/))([A-Za-z0-9_-]{11})"
)
_VIMEO_ID = re.compile(r"(?:player\.)?vimeo\.com/(?:video/)?(\d+)")
_CHUNK_BYTES = 1024 * 1024
_COLUMNS = {
    "caption": "Caption",
    "alt_text": "AltText",
    "taken_on": "TakenOn",
    "is_published": "IsPublished",
}


def gallery_link(url: str) -> str:
    """A public image or video address. Gallery links are stored as given and are never downloaded.

    A chosen picture frame is kept on the address as #cplcrop=x,y,w,h. Those four numbers are
    fractions of the picture, from the top left. The file at the link is not copied or cut.
    """
    return normalize_picture_link((url or "").strip())


def media_kind(url: str) -> str:
    """image or video, from a YouTube or Vimeo address, a direct video file, or anything else."""
    text = url or ""
    if _YOUTUBE_ID.search(text) or _VIMEO_ID.search(text):
        return "video"
    path = urlparse(text.split("#", 1)[0]).path.lower()
    if path.endswith((".mp4", ".webm", ".mov", ".m4v", ".ogg")) or "/video/upload/" in text.lower():
        return "video"
    return "image"


def video_poster(url: str) -> str | None:
    match = _YOUTUBE_ID.search(url or "")
    if not match:
        return None
    return f"https://i.ytimg.com/vi/{match.group(1)}/hqdefault.jpg"


def _is_video_bytes(data: bytes) -> bool:
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return True
    return data.startswith(b"\x1a\x45\xdf\xa3")


def read_gallery_upload(stream) -> tuple[bytes, str]:
    """Read one uploaded photo or video. Images stay at 5 MB. Videos may be 50 MB."""
    chunks = []
    total = 0
    while True:
        chunk = stream.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_VIDEO_BYTES:
            raise HTTPException(status_code=400, detail="The file must be 50 MB or smaller.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="Choose a photo or a video to upload.")
    if image_format(data):
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Image must be 5 MB or smaller.")
        return data, "image"
    if _is_video_bytes(data):
        return data, "video"
    raise HTTPException(status_code=400, detail="Use a JPEG, PNG, WebP, MP4, WebM or MOV file.")


def image_pixel_size(data: bytes) -> tuple[int, int] | None:
    kind = image_format(data)
    if kind == "png" and len(data) >= 24 and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return int(width), int(height)
    if kind == "jpeg":
        return _jpeg_size(data)
    if kind == "webp":
        return _webp_size(data)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    index = 2
    while index + 8 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xC0, 0xC1, 0xC2}:
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        if marker in {0xD8, 0xD9}:
            index += 2
            continue
        if index + 4 > len(data):
            return None
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        return 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
    if chunk == b"VP8 ":
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(data) >= 25:
        first, second, third, fourth = data[21:25]
        width = 1 + (((second & 0x3F) << 8) | first)
        height = 1 + (((fourth & 0x0F) << 10) | (third << 2) | ((second & 0xC0) >> 6))
        return width, height
    return None


def encode_cursor(sort_order: int, item_id: int, *, newest: bool = False) -> str:
    if newest:
        return f"n:{int(item_id)}"
    return f"p:{int(sort_order)}:{int(item_id)}"


def decode_cursor(cursor: str | None, *, newest: bool = False):
    if not cursor:
        return None
    parts = cursor.split(":")
    try:
        if newest:
            if len(parts) != 2 or parts[0] != "n":
                raise ValueError
            return int(parts[1])
        if len(parts) != 3 or parts[0] != "p":
            raise ValueError
        return int(parts[1]), int(parts[2])
    except ValueError:
        raise HTTPException(status_code=400, detail="That gallery page cursor is not valid.") from None


def page_by_position(rows: list[dict], cursor: str | None, limit: int) -> tuple[list[dict], str | None]:
    """Keep rows already ordered by (SortOrder, Id). The next page starts after the cursor."""
    after = decode_cursor(cursor, newest=False)
    chosen = []
    for row in rows:
        sort_order = int(row["SortOrder"])
        item_id = int(row["Id"])
        if after is not None:
            after_sort, after_id = after
            if sort_order < after_sort or (sort_order == after_sort and item_id <= after_id):
                continue
        chosen.append(row)
        if len(chosen) > limit:
            break
    page = chosen[:limit]
    next_cursor = encode_cursor(int(page[-1]["SortOrder"]), int(page[-1]["Id"])) if len(chosen) > limit else None
    return page, next_cursor


def _clean_text(value: str | None, limit: int, label: str) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > limit:
        raise HTTPException(status_code=400, detail=f"{label} must be {limit} characters or fewer.")
    return text


def _checked_size(width: int | None, height: int | None, data: bytes | None = None) -> tuple[int, int]:
    sniffed = image_pixel_size(data) if data else None
    if sniffed:
        width, height = sniffed
    if width is None or height is None or width < 1 or height < 1 or width > _MAX_EDGE or height > _MAX_EDGE:
        raise HTTPException(status_code=400, detail="The image width and height are required.")
    return int(width), int(height)


def _video_frame(width: int | None, height: int | None) -> tuple[int, int]:
    if (
        width is not None
        and height is not None
        and 1 <= int(width) <= _MAX_EDGE
        and 1 <= int(height) <= _MAX_EDGE
    ):
        return int(width), int(height)
    return _VIDEO_FRAME


def _like_pattern(caption: str | None) -> str:
    text = (caption or "").strip()
    if not text:
        return ""
    escaped = text.replace("[", "[[]").replace("%", "[%]").replace("_", "[_]")
    return f"%{escaped}%"


class GalleryService:
    def __init__(self, repository, storage=None):
        self.repository = repository
        self.storage = storage

    def public_seasons(self) -> list[GallerySeason]:
        seasons = []
        for row in self.repository.public_seasons():
            cover = None
            if int(row.get("PhotoCount") or 0) < 1:
                continue
            if row.get("CoverId") is not None:
                cover = self._photo(
                    {
                        "Id": row["CoverId"],
                        "SourceType": row.get("CoverSource") or "link",
                        "MediaType": row.get("CoverMedia") or "image",
                        "Url": row["CoverUrl"],
                        "StorageKey": row.get("CoverKey"),
                        "Width": row["CoverWidth"],
                        "Height": row["CoverHeight"],
                        "Caption": row.get("CoverCaption"),
                        "AltText": row.get("CoverAlt"),
                        "TakenOn": row.get("CoverTakenOn"),
                        "IsPublished": 1,
                        "SortOrder": row.get("CoverSort") or 0,
                        "IsCover": 1,
                    },
                    [int(row["Id"])],
                )
            seasons.append(
                GallerySeason(
                    id=int(row["Id"]),
                    name=row["Name"],
                    photo_count=int(row["PhotoCount"] or 0),
                    cover=cover,
                )
            )
        return seasons

    def public_page(self, season_id: int, cursor: str | None, limit: int) -> GalleryPage:
        limit = _limit(limit)
        if not self.repository.season_is_public(season_id):
            return GalleryPage(items=[], next_cursor=None)
        after = decode_cursor(cursor, newest=False)
        after_sort, after_id = after if after else (None, None)
        rows = self.repository.list_public(season_id, after_sort, after_id, limit + 1)
        return self._page(rows, limit, newest=False, public_only=True)

    def public_photo(self, item_id: int) -> GalleryPhoto:
        row = self.repository.get_public(item_id)
        if row is None:
            raise HTTPException(status_code=404, detail="That photo is not on the public gallery.")
        seasons = self.repository.season_ids_for([item_id], public_only=True).get(item_id, [])
        return self._photo(row, seasons)

    def admin_page(self, season_id: int | None, status: str, caption: str | None, cursor: str | None, limit: int) -> GalleryPage:
        limit = _limit(limit, default=48, maximum=100)
        newest = not season_id
        if newest:
            after_id = decode_cursor(cursor, newest=True)
            after_sort = None
        else:
            after = decode_cursor(cursor, newest=False)
            after_sort, after_id = after if after else (None, None)
        rows = self.repository.list_admin(
            season_id,
            status or "all",
            _like_pattern(caption),
            after_sort,
            after_id,
            limit + 1,
        )
        return self._page(rows, limit, newest=newest, public_only=False)

    def create(
        self,
        admin,
        *,
        file: UploadFile | None,
        url: str | None,
        season_ids: str | list[int],
        caption: str | None,
        alt_text: str | None,
        width: int | None,
        height: int | None,
        taken_on: date | None,
    ) -> GalleryPhoto:
        seasons = self._seasons(season_ids, allow_empty=True)
        filename = getattr(file, "filename", None) if file is not None else None
        link = (url or "").strip()
        if filename and link:
            raise HTTPException(status_code=400, detail="Send a file or a public link, not both.")
        stored: StoredImage | None = None
        if filename:
            if self.storage is None:
                raise HTTPException(status_code=503, detail="Media storage is not configured on the server.")
            data, media_type = read_gallery_upload(getattr(file, "file", file))
            if media_type == "video":
                width, height = _video_frame(width, height)
                stored = self.storage.upload_video(data)
            else:
                width, height = _checked_size(width, height, data)
                stored = self.storage.upload(data, "gallery")
            source = {
                "source_type": "upload",
                "media_type": media_type,
                "url": stored.url,
                "storage_key": stored.key,
            }
        elif link:
            clean = gallery_link(link)
            media_type = media_kind(clean)
            if media_type == "video":
                width, height = _video_frame(width, height)
            else:
                width, height = _checked_size(width, height)
            source = {"source_type": "link", "media_type": media_type, "url": clean, "storage_key": None}
        else:
            raise HTTPException(status_code=400, detail="Choose a photo, a video, or a public link.")
        row = {
            **source,
            "width": width,
            "height": height,
            "caption": _clean_text(caption, 500, "Caption"),
            "alt_text": _clean_text(alt_text, 300, "Alt text"),
            "taken_on": taken_on,
            "admin_id": admin.id,
        }
        item_id = None
        try:
            item_id = self.repository.insert(row)
            self.repository.replace_seasons(item_id, seasons)
        except Exception:
            if item_id is not None:
                self.repository.delete(item_id)
            if stored is not None:
                self._forget(stored.key)
            raise
        return self._saved(item_id)

    def update(self, item_id: int, payload: GalleryUpdateRequest, admin, provided: set[str]) -> GalleryPhoto:
        current = self.repository.get(item_id)
        if current is None:
            raise HTTPException(status_code=404, detail="That photo is not in the gallery.")
        fields = {}
        if "caption" in provided:
            fields["Caption"] = _clean_text(payload.caption, 500, "Caption")
        if "alt_text" in provided:
            fields["AltText"] = _clean_text(payload.alt_text, 300, "Alt text")
        if payload.clear_taken_on:
            fields["TakenOn"] = None
        elif "taken_on" in provided:
            fields["TakenOn"] = payload.taken_on
        if "is_published" in provided and payload.is_published is not None:
            self._need(admin, "gallery.publish")
            fields["IsPublished"] = 1 if payload.is_published else 0
        seasons = None
        if "season_ids" in provided:
            seasons = self._seasons(payload.season_ids or [], allow_empty=True)
        if not fields and seasons is None:
            raise HTTPException(status_code=400, detail="Nothing to change.")
        self.repository.update(item_id, fields, admin.id)
        if seasons is not None:
            self.repository.replace_seasons(item_id, seasons)
        return self._saved(item_id)

    def order(self, payload: GalleryOrderRequest) -> GalleryPage:
        if not self.repository.known_seasons([payload.season_id]):
            raise HTTPException(status_code=400, detail="Choose a season that exists.")
        existing = self.repository.item_ids_for_season(payload.season_id)
        if len(payload.item_ids) != len(set(payload.item_ids)) or set(payload.item_ids) != set(existing):
            raise HTTPException(status_code=400, detail="Send every photo in that season, once each.")
        self.repository.set_order(payload.season_id, payload.item_ids)
        return self.admin_page(payload.season_id, "all", None, None, max(len(payload.item_ids), 1))

    def cover(self, season_id: int, payload: GalleryCoverRequest) -> GalleryPhoto:
        if not self.repository.set_cover(season_id, payload.item_id):
            raise HTTPException(status_code=404, detail="That photo is not in this season.")
        return self._saved(payload.item_id)

    def bulk(self, payload: GalleryBulkRequest, admin) -> GalleryBulkResult:
        ids = list(dict.fromkeys(payload.ids))
        if not ids or len(ids) > _MAX_BULK:
            raise HTTPException(status_code=400, detail=f"Choose between 1 and {_MAX_BULK} photos.")
        if any(item_id < 1 for item_id in ids):
            raise HTTPException(status_code=400, detail="Choose photos that exist.")
        if payload.action in {"publish", "hide"}:
            self._need(admin, "gallery.publish")
            updated = self.repository.set_published(ids, payload.action == "publish", admin.id)
            return GalleryBulkResult(updated=updated)
        if payload.action == "add_seasons":
            seasons = self._seasons(payload.season_ids)
            return GalleryBulkResult(updated=self.repository.add_seasons(ids, seasons))
        self._need(admin, "gallery.delete")
        rows = self.repository.delete_many(ids)
        for row in rows:
            self._forget(row.get("StorageKey"))
        return GalleryBulkResult(updated=len(rows))

    def delete(self, item_id: int, admin) -> None:
        row = self.repository.delete(item_id)
        if row is None:
            raise HTTPException(status_code=404, detail="That photo is not in the gallery.")
        self._forget(row.get("StorageKey"))

    def _saved(self, item_id: int) -> GalleryPhoto:
        row = self.repository.get(item_id)
        if row is None:
            raise HTTPException(status_code=404, detail="That photo is not in the gallery.")
        seasons = self.repository.season_ids_for([item_id]).get(item_id, [])
        return self._photo(row, seasons)

    def _page(self, rows: list[dict], limit: int, *, newest: bool, public_only: bool) -> GalleryPage:
        extra = len(rows) > limit
        page = rows[:limit]
        seasons = self.repository.season_ids_for([int(row["Id"]) for row in page], public_only=public_only)
        items = [self._photo(row, seasons.get(int(row["Id"]), [])) for row in page]
        next_cursor = None
        if extra and page:
            last = page[-1]
            next_cursor = encode_cursor(int(last["SortOrder"]), int(last["Id"]), newest=newest)
        return GalleryPage(items=items, next_cursor=next_cursor)

    def _photo(self, row: dict, season_ids: list[int]) -> GalleryPhoto:
        url = row["Url"]
        key = row.get("StorageKey")
        media_type = row.get("MediaType") or "image"
        if media_type not in {"image", "video"}:
            media_type = "image"
        thumbnail = url
        placeholder = None
        if key and self.storage is not None and self.storage.owns(key):
            variants = self.storage.gallery_variants(key)
            thumbnail = variants["thumbnail_url"]
            placeholder = variants["placeholder_url"]
        elif media_type == "video":
            poster = video_poster(url)
            if poster:
                thumbnail = poster
        taken = row.get("TakenOn")
        if isinstance(taken, datetime):
            taken = taken.date()
        return GalleryPhoto(
            id=int(row["Id"]),
            source_type=row["SourceType"],
            media_type=media_type,
            url=url,
            thumbnail_url=thumbnail,
            placeholder_url=placeholder,
            width=int(row["Width"]),
            height=int(row["Height"]),
            caption=row.get("Caption"),
            alt_text=row.get("AltText"),
            taken_on=taken,
            sort_order=int(row.get("SortOrder") or 0),
            is_cover=bool(row.get("IsCover")),
            is_published=bool(row.get("IsPublished")),
            season_ids=season_ids,
        )

    def _seasons(self, raw, *, allow_empty: bool = False) -> list[int]:
        if isinstance(raw, str):
            parts = [part.strip() for part in raw.split(",") if part.strip()]
            if any(not part.isdigit() for part in parts):
                raise HTTPException(status_code=400, detail="Choose a season for this photo.")
            season_ids = [int(part) for part in parts]
        else:
            season_ids = [int(part) for part in (raw or [])]
        season_ids = list(dict.fromkeys(season_ids))
        if not season_ids and allow_empty:
            return []
        if not season_ids and isinstance(raw, str):
            raise HTTPException(status_code=400, detail="Choose a season for this photo.")
        if not season_ids or len(season_ids) > _MAX_SEASONS:
            raise HTTPException(status_code=400, detail=f"Choose between 1 and {_MAX_SEASONS} seasons.")
        known = self.repository.known_seasons(season_ids)
        if set(season_ids) - set(known):
            raise HTTPException(status_code=400, detail="Choose a season that exists.")
        return season_ids

    def _forget(self, key: str | None) -> None:
        if not key or self.storage is None or not self.storage.owns(key):
            return
        try:
            self.storage.delete(key)
        except Exception:
            logger.exception("Could not delete stored gallery image")

    @staticmethod
    def _need(admin, code: str) -> None:
        if code not in admin.permissions:
            raise HTTPException(status_code=403, detail=denied_message((code,)))


def _limit(limit: int | None, default: int = 24, maximum: int = 60) -> int:
    if limit is None:
        return default
    if limit < 1 or limit > maximum:
        raise HTTPException(status_code=400, detail=f"Ask for between 1 and {maximum} photos.")
    return limit
