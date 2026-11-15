import io
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional, Protocol
from urllib.parse import urlparse

from core.config import settings

ImageKind = Literal["player", "team", "gallery", "admin"]

# Incoming transformations run once at upload, so delivery never pays for them.
_INCOMING = {
    "player": [{"width": 512, "height": 512, "crop": "fill", "gravity": "face"}],
    "team": [{"width": 512, "height": 512, "crop": "limit"}],
    "gallery": [{"width": 2560, "crop": "limit"}],
    "admin": [{"width": 512, "height": 512, "crop": "fill", "gravity": "face"}],
}
_SUBFOLDER = {"player": "players", "team": "teams", "gallery": "gallery", "admin": "admins"}


@dataclass(frozen=True)
class StoredImage:
    url: str
    key: str


class MediaStorage(Protocol):
    def upload(self, data: bytes, kind: ImageKind) -> StoredImage: ...

    def owns(self, key: str) -> bool: ...

    def delete(self, key: str) -> None: ...

    def default_url(self, kind: ImageKind) -> str: ...


def default_key(folder: str, kind: ImageKind) -> str:
    return f"{folder}/defaults/{kind}"


# public_id values this app creates: "<folder>/players|teams|gallery|videos|admins/<32 hex>".
_OWNED_KEY = re.compile(r"^(?P<folder>.+)/(?P<kind>players|teams|gallery|videos|admins)/(?P<id>[0-9a-f]{32})$")


def is_owned_image_key(key: object, folder: str) -> bool:
    """True when the key is a Cloudinary image this app uploaded, not a link or a default."""
    if not isinstance(key, str):
        return False
    folder = (folder or "").strip("/")
    if not folder or "://" in key or ".." in key:
        return False
    match = _OWNED_KEY.fullmatch(key.strip())
    return bool(match and match.group("folder") == folder)


class CloudinaryStorage:
    def __init__(self, cloudinary_url: str, folder: str):
        import cloudinary

        parsed = urlparse(cloudinary_url)
        if parsed.scheme != "cloudinary" or not parsed.hostname or not parsed.username or not parsed.password:
            raise RuntimeError("CPL_CLOUDINARY_URL must look like cloudinary://<api_key>:<api_secret>@<cloud_name>.")
        cloudinary.config(
            cloud_name=parsed.hostname,
            api_key=parsed.username,
            api_secret=parsed.password,
            secure=True,
        )
        self.folder = folder

    @staticmethod
    def _delivery_url(public_id: str, version: Optional[int] = None) -> str:
        from cloudinary.utils import cloudinary_url

        options = {"secure": True, "fetch_format": "auto", "quality": "auto"}
        if version:
            options["version"] = version
        return cloudinary_url(public_id, **options)[0]

    def upload(self, data: bytes, kind: ImageKind) -> StoredImage:
        import cloudinary.uploader

        result = cloudinary.uploader.upload(
            io.BytesIO(data),
            resource_type="image",
            folder=f"{self.folder}/{_SUBFOLDER[kind]}",
            public_id=uuid.uuid4().hex,
            overwrite=False,
            transformation=_INCOMING[kind],
        )
        public_id = result["public_id"]
        return StoredImage(url=self._delivery_url(public_id, result.get("version")), key=public_id)

    def owns(self, key: str) -> bool:
        return is_owned_image_key(key, self.folder)

    def delete(self, key: str) -> None:
        if not self.owns(key):
            return
        import cloudinary.uploader

        resource = "video" if "/videos/" in key else "image"
        cloudinary.uploader.destroy(key, resource_type=resource, invalidate=True)

    def upload_video(self, data: bytes) -> StoredImage:
        import cloudinary.uploader

        result = cloudinary.uploader.upload(
            io.BytesIO(data),
            resource_type="video",
            folder=f"{self.folder}/videos",
            public_id=uuid.uuid4().hex,
            overwrite=False,
        )
        public_id = result["public_id"]
        from cloudinary.utils import cloudinary_url

        url = cloudinary_url(public_id, resource_type="video", secure=True, format="mp4")[0]
        return StoredImage(url=url, key=public_id)

    def default_url(self, kind: ImageKind) -> str:
        return self._delivery_url(default_key(self.folder, kind))

    def gallery_variants(self, public_id: str) -> dict[str, str]:
        """Small delivery URLs derived from an upload this app owns. The stored URL stays full size."""
        if "/videos/" in public_id:
            return {
                "thumbnail_url": self._video_poster(public_id, [{"width": 640, "crop": "limit"}]),
                "placeholder_url": self._video_poster(
                    public_id,
                    [{"width": 40, "crop": "limit", "effect": "blur:1000", "quality": 30}],
                ),
            }
        return {
            "thumbnail_url": self._transformed(public_id, [{"width": 640, "crop": "limit"}]),
            "placeholder_url": self._transformed(
                public_id,
                [{"width": 40, "crop": "limit", "effect": "blur:1000", "quality": 30}],
            ),
        }

    @staticmethod
    def _video_poster(public_id: str, transformation: list) -> str:
        from cloudinary.utils import cloudinary_url

        return cloudinary_url(
            public_id,
            resource_type="video",
            format="jpg",
            secure=True,
            transformation=transformation,
        )[0]

    @staticmethod
    def _transformed(public_id: str, transformation: list) -> str:
        from cloudinary.utils import cloudinary_url

        return cloudinary_url(
            public_id,
            secure=True,
            fetch_format="auto",
            quality="auto",
            transformation=transformation,
        )[0]


@lru_cache()
def _configured_storage() -> Optional[CloudinaryStorage]:
    if not settings.CLOUDINARY_URL:
        return None
    return CloudinaryStorage(settings.CLOUDINARY_URL, settings.MEDIA_FOLDER)


def get_storage() -> Optional[MediaStorage]:
    return _configured_storage()
