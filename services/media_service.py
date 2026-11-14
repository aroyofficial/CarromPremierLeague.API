import logging
from typing import BinaryIO, Callable, Optional

from fastapi import HTTPException

from core.storage import ImageKind, MediaStorage
from schemas.media_schema import MediaDefaultsResponse

MAX_IMAGE_BYTES = 5 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024

logger = logging.getLogger(__name__)


def image_format(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def read_image(stream: BinaryIO) -> bytes:
    """Read an uploaded image, refusing anything over the size cap or not JPEG, PNG or WebP."""
    chunks = []
    total = 0
    while True:
        chunk = stream.read(_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail="Image must be 5 MB or smaller.")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(status_code=400, detail="Choose an image to upload.")
    if image_format(data) is None:
        raise HTTPException(status_code=400, detail="Image must be a JPEG, PNG or WebP file.")
    return data


class MediaService:

    def __init__(self, storage: Optional[MediaStorage], player_repository=None, team_repository=None):
        self.storage = storage
        self.player_repository = player_repository
        self.team_repository = team_repository

    def _require_storage(self) -> MediaStorage:
        if self.storage is None:
            raise HTTPException(status_code=503, detail="Picture storage is not configured on the server.")
        return self.storage

    def defaults(self) -> MediaDefaultsResponse:
        if self.storage is None:
            return MediaDefaultsResponse()
        return MediaDefaultsResponse(
            player=self.storage.default_url("player"),
            team=self.storage.default_url("team"),
            uploads_enabled=True,
        )

    def _forget(self, key: Optional[str]) -> None:
        if not key or self.storage is None:
            return
        try:
            self.storage.delete(key)
        except Exception:
            logger.exception("Could not delete stored image %s; it is now unused.", key)

    def _replace(
        self,
        kind: ImageKind,
        label: str,
        get_key: Callable[[], tuple[bool, Optional[str]]],
        save: Callable[[Optional[str], Optional[str]], bool],
        data: bytes,
    ) -> None:
        storage = self._require_storage()
        exists, old_key = get_key()
        if not exists:
            raise HTTPException(status_code=404, detail=f"{label} not found.")

        try:
            stored = storage.upload(data, kind)
        except HTTPException:
            raise
        except Exception:
            logger.exception("Image upload to storage failed.")
            raise HTTPException(status_code=502, detail="The picture could not be uploaded. Try again.")

        try:
            saved = save(stored.url, stored.key)
        except Exception:
            self._forget(stored.key)
            raise
        if not saved:
            self._forget(stored.key)
            raise HTTPException(status_code=404, detail=f"{label} not found.")

        self._forget(old_key)

    def _remove(
        self,
        label: str,
        get_key: Callable[[], tuple[bool, Optional[str]]],
        save: Callable[[Optional[str], Optional[str]], bool],
    ) -> None:
        exists, old_key = get_key()
        if not exists:
            raise HTTPException(status_code=404, detail=f"{label} not found.")
        if old_key:
            self._require_storage()
        save(None, None)
        self._forget(old_key)

    def set_player_avatar(self, player_id: int, data: bytes):
        repository = self.player_repository
        self._replace(
            "player",
            "Player",
            lambda: repository.get_avatar_key(player_id),
            lambda url, key: repository.set_avatar(player_id, url, key),
            data,
        )
        return repository.get_by_id(player_id)

    def remove_player_avatar(self, player_id: int):
        repository = self.player_repository
        self._remove(
            "Player",
            lambda: repository.get_avatar_key(player_id),
            lambda url, key: repository.set_avatar(player_id, url, key),
        )
        return repository.get_by_id(player_id)

    def set_team_logo(self, team_id: int, data: bytes):
        repository = self.team_repository
        self._replace(
            "team",
            "Team",
            lambda: repository.get_logo_key(team_id),
            lambda url, key: repository.set_logo(team_id, url, key),
            data,
        )
        return repository.get_by_id(team_id)

    def remove_team_logo(self, team_id: int):
        repository = self.team_repository
        self._remove(
            "Team",
            lambda: repository.get_logo_key(team_id),
            lambda url, key: repository.set_logo(team_id, url, key),
        )
        return repository.get_by_id(team_id)
