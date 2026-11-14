import ipaddress
import logging
import re
import socket
import urllib.request
from typing import BinaryIO, Callable, Optional
from urllib.parse import urlparse

from fastapi import HTTPException, UploadFile

from core.config import settings
from core.storage import ImageKind, MediaStorage, is_owned_image_key
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


def _resolve_host(host: str) -> list[str]:
    addresses = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family in (socket.AF_INET, socket.AF_INET6):
            addresses.append(sockaddr[0])
    return addresses


def _require_public_url(url: str, resolve: Callable[[str], list[str]]) -> str:
    text = (url or "").strip()
    if not text or len(text) > 2048:
        raise HTTPException(status_code=400, detail="Enter a public image link.")
    parsed = urlparse(text)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="Enter a public image link starting with http:// or https://.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Enter a public link without a username or password.")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost") or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="Use a public image link.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [literal]
    else:
        try:
            host.encode("idna")
            resolved = resolve(host)
        except (OSError, UnicodeError):
            raise HTTPException(status_code=400, detail="That link could not be reached.")
        addresses = []
        for item in resolved:
            try:
                addresses.append(ipaddress.ip_address(item))
            except ValueError:
                raise HTTPException(status_code=400, detail="That link could not be reached.")
        if not addresses:
            raise HTTPException(status_code=400, detail="That link could not be reached.")
    if any(not address.is_global for address in addresses):
        raise HTTPException(status_code=400, detail="Use a public image link.")
    return text


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, resolve: Callable[[str], list[str]]):
        super().__init__()
        self._resolve = resolve
        self.max_redirections = 4
        self.max_repeats = 2

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _require_public_url(newurl, self._resolve)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _header(headers, name: str) -> str:
    if headers is None or not hasattr(headers, "get"):
        return ""
    value = headers.get(name)
    if not value:
        value = headers.get(name.lower())
    return str(value or "")


def _as_public_image(stream: BinaryIO, headers) -> bytes:
    media_type = _header(headers, "Content-Type").split(";")[0].strip().lower()
    if media_type.startswith("text/") or media_type in {"application/json", "application/xml", "application/xhtml+xml"}:
        raise HTTPException(status_code=400, detail="That link is not an image.")
    length = _header(headers, "Content-Length")
    if length.isdigit() and int(length) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=400, detail="That image is larger than 5 MB.")
    try:
        return read_image(stream)
    except HTTPException as error:
        detail = str(error.detail)
        if "5 MB" in detail:
            rewritten = "That image is larger than 5 MB."
        elif "Choose an image" in detail:
            rewritten = "That link did not return an image."
        else:
            rewritten = "That link is not a JPEG, PNG or WebP image."
        raise HTTPException(status_code=400, detail=rewritten) from error


def fetch_public_image(
    url: str,
    *,
    opener=None,
    resolve: Optional[Callable[[str], list[str]]] = None,
) -> bytes:
    """Download a public http(s) image. Private hosts, local addresses and redirects to them are refused."""
    resolver = resolve or _resolve_host
    checked = _require_public_url(url, resolver)
    if opener is None:
        opener = urllib.request.build_opener(_GuardedRedirectHandler(resolver))
    request = urllib.request.Request(
        checked,
        headers={
            "User-Agent": "CarromPremierLeague/1.0",
            "Accept": "image/jpeg,image/png,image/webp,image/*;q=0.8",
        },
        method="GET",
    )
    try:
        with opener.open(request, timeout=20) as response:
            return _as_public_image(response, getattr(response, "headers", {}))
    except HTTPException:
        raise
    except Exception as error:
        logger.warning("Could not download a public image (%s).", type(error).__name__)
        raise HTTPException(status_code=400, detail="That link could not be downloaded.") from error


_CROP = re.compile(r"^cplcrop=([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+)$")


def normalize_picture_link(url: str) -> str:
    """Keep a public image address and an optional chosen frame. The image itself is not downloaded."""
    text = (url or "").strip()
    if not text or len(text) > 2048:
        raise HTTPException(status_code=400, detail="Enter a public image link.")
    base, _, fragment = text.partition("#")
    parsed = urlparse(base)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise HTTPException(status_code=400, detail="Enter a public image link starting with http:// or https://.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="Enter a public link without a username or password.")
    if host in {"localhost", "metadata.google.internal"} or host.endswith(".localhost") or host.endswith(".local"):
        raise HTTPException(status_code=400, detail="Use a public image link.")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise HTTPException(status_code=400, detail="Use a public image link.")
    crop = ""
    if fragment:
        match = _CROP.fullmatch(fragment)
        if not match:
            raise HTTPException(status_code=400, detail="That picture frame is not valid.")
        numbers = tuple(float(part) for part in match.groups())
        if any(number < 0 or number > 1 for number in numbers) or numbers[2] <= 0 or numbers[3] <= 0:
            raise HTTPException(status_code=400, detail="That picture frame is not valid.")
        if numbers[0] + numbers[2] > 1.001 or numbers[1] + numbers[3] > 1.001:
            raise HTTPException(status_code=400, detail="That picture frame is not valid.")
        crop = "#cplcrop=" + ",".join(f"{number:.4f}" for number in numbers)
    stored = f"{base}{crop}"
    if len(stored) > 2048:
        raise HTTPException(status_code=400, detail="That link is too long.")
    return stored


def read_submitted_picture(
    file: Optional[UploadFile],
    url: Optional[str],
    *,
    fetch: Callable[[str], bytes] = fetch_public_image,
) -> bytes:
    filename = getattr(file, "filename", None) if file is not None else None
    link = (url or "").strip()
    if filename and link:
        raise HTTPException(status_code=400, detail="Send an image file or a public link, not both.")
    if filename:
        return read_image(getattr(file, "file", file))
    if link:
        return fetch(link)
    raise HTTPException(status_code=400, detail="Choose an image file or a public image link.")


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
        if not self.storage.owns(key):
            logger.info("Left the previous picture in place; it is not a Cloudinary image from this app.")
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
        if old_key and self.storage is None and is_owned_image_key(old_key, settings.MEDIA_FOLDER):
            self._require_storage()
        save(None, None)
        self._forget(old_key)

    def _store_link(
        self,
        label: str,
        get_key: Callable[[], tuple[bool, Optional[str]]],
        save: Callable[[Optional[str], Optional[str]], bool],
        url: str,
    ):
        stored = normalize_picture_link(url)
        exists, old_key = get_key()
        if not exists:
            raise HTTPException(status_code=404, detail=f"{label} not found.")
        if old_key and self.storage is None and is_owned_image_key(old_key, settings.MEDIA_FOLDER):
            self._require_storage()
        if not save(stored, None):
            raise HTTPException(status_code=404, detail=f"{label} not found.")
        self._forget(old_key)

    def apply_player_picture(self, player_id: int, file, url: Optional[str]):
        link = (url or "").strip()
        filename = getattr(file, "filename", None) if file is not None else None
        if filename and link:
            raise HTTPException(status_code=400, detail="Send an image file or a public link, not both.")
        if link:
            return self.set_player_avatar_link(player_id, link)
        if filename:
            return self.set_player_avatar(player_id, read_image(getattr(file, "file", file)))
        raise HTTPException(status_code=400, detail="Choose an image file or a public image link.")

    def apply_team_logo(self, team_id: int, file, url: Optional[str]):
        link = (url or "").strip()
        filename = getattr(file, "filename", None) if file is not None else None
        if filename and link:
            raise HTTPException(status_code=400, detail="Send an image file or a public link, not both.")
        if link:
            return self.set_team_logo_link(team_id, link)
        if filename:
            return self.set_team_logo(team_id, read_image(getattr(file, "file", file)))
        raise HTTPException(status_code=400, detail="Choose an image file or a public image link.")

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

    def set_player_avatar_link(self, player_id: int, url: str):
        repository = self.player_repository
        self._store_link(
            "Player",
            lambda: repository.get_avatar_key(player_id),
            lambda stored, key: repository.set_avatar(player_id, stored, key),
            url,
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

    def set_team_logo_link(self, team_id: int, url: str):
        repository = self.team_repository
        self._store_link(
            "Team",
            lambda: repository.get_logo_key(team_id),
            lambda stored, key: repository.set_logo(team_id, stored, key),
            url,
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
