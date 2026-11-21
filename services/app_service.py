import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status

from core.permissions import (
    ALL_CODES,
    APP_ALLOWED_SCOPES,
    APP_FORBIDDEN_SCOPES,
    AREA_NAMES,
    CATALOG,
    missing_parent_message,
    missing_parents,
    ordered,
)
from schemas.app_schema import (
    AppCreatedResponse,
    AppCreateRequest,
    AppIdentityResponse,
    AppListResponse,
    AppResponse,
    AppScopeOption,
    AppUpdateRequest,
)
from schemas.auth_schema import AdminResponse

KEY_PREFIX = "cpl_live_"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str, str]:
    raw_key = f"{KEY_PREFIX}{secrets.token_urlsafe(32)}"
    prefix = raw_key[:17]
    return raw_key, prefix, hash_api_key(raw_key)


def available_app_scopes() -> list[AppScopeOption]:
    return [
        AppScopeOption(
            code=p.code,
            area=p.area,
            area_name=AREA_NAMES[p.area],
            name=p.name,
            description=p.description,
            parent=p.parent,
        )
        for p in CATALOG
        if p.code in APP_ALLOWED_SCOPES
    ]


class AppService:
    def __init__(self, repository):
        self.repository = repository

    @staticmethod
    def _validate_scopes(codes: list[str], actor: AdminResponse) -> list[str]:
        unknown = sorted({code for code in codes if code not in ALL_CODES})
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown permission: {', '.join(unknown)}.")
        forbidden = sorted({code for code in codes if code in APP_FORBIDDEN_SCOPES})
        if forbidden:
            raise HTTPException(
                status_code=400,
                detail=f"API keys cannot hold account or integration permissions: {', '.join(forbidden)}.",
            )
        wanted = set(codes)
        missing_actor = wanted - set(actor.permissions)
        if missing_actor:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only grant permissions to an app that you currently hold.",
            )
        missing_tree = missing_parents(wanted)
        if missing_tree:
            raise HTTPException(status_code=400, detail=missing_parent_message(missing_tree))
        return ordered(wanted)

    @staticmethod
    def _validate_expiry(expires_at: datetime | None) -> datetime | None:
        if expires_at is None:
            return None
        normalized = _as_utc(expires_at)
        if normalized is not None and normalized <= _utcnow():
            raise HTTPException(status_code=400, detail="Expiration date must be in the future.")
        return normalized.replace(tzinfo=None) if normalized else None

    @staticmethod
    def _to_response(record: dict) -> AppResponse:
        return AppResponse(
            id=record["id"],
            name=record["name"],
            description=record.get("description"),
            key_prefix=record["key_prefix"],
            permissions=ordered(c for c in record.get("permissions", []) if c in APP_ALLOWED_SCOPES),
            is_active=bool(record.get("is_active", True)),
            expires_at=record.get("expires_at"),
            last_used_at=record.get("last_used_at"),
            created_at=record.get("created_at"),
            updated_at=record.get("updated_at"),
            created_by=int(record["created_by"]),
        )

    def list_apps(self) -> AppListResponse:
        rows = self.repository.list_all()
        return AppListResponse(
            apps=[self._to_response(row) for row in rows],
            available_scopes=available_app_scopes(),
        )

    def create_app(self, request: AppCreateRequest, actor: AdminResponse) -> AppCreatedResponse:
        scopes = self._validate_scopes(request.permissions, actor)
        expires_at = self._validate_expiry(request.expires_at)
        raw_key, prefix, key_hash = generate_api_key()
        created = self.repository.create(
            name=request.name,
            description=request.description,
            key_prefix=prefix,
            key_hash=key_hash,
            permissions=scopes,
            expires_at=expires_at,
            created_by=actor.id,
        )
        base = self._to_response(created)
        return AppCreatedResponse(**base.model_dump(), api_key=raw_key)

    def update_app(self, app_id: int, request: AppUpdateRequest, actor: AdminResponse) -> AppResponse:
        existing = self.repository.get_by_id(app_id)
        if not existing:
            raise HTTPException(status_code=404, detail="App not found.")
        changes = request.model_dump(exclude_unset=True)
        if "permissions" in changes:
            if changes["permissions"] is None:
                changes.pop("permissions")
            else:
                changes["permissions"] = self._validate_scopes(changes["permissions"], actor)
        if "expires_at" in changes and changes["expires_at"] is not None:
            changes["expires_at"] = self._validate_expiry(changes["expires_at"])
        updated = self.repository.update(app_id, changes)
        if not updated:
            raise HTTPException(status_code=404, detail="App not found.")
        return self._to_response(updated)

    def rotate_key(self, app_id: int) -> AppCreatedResponse:
        existing = self.repository.get_by_id(app_id)
        if not existing:
            raise HTTPException(status_code=404, detail="App not found.")
        raw_key, prefix, key_hash = generate_api_key()
        rotated = self.repository.rotate_key(app_id, prefix, key_hash)
        if not rotated:
            raise HTTPException(status_code=404, detail="App not found.")
        base = self._to_response(rotated)
        return AppCreatedResponse(**base.model_dump(), api_key=raw_key)

    def revoke_app(self, app_id: int) -> bool:
        if not self.repository.revoke(app_id):
            raise HTTPException(status_code=404, detail="App not found.")
        return True

    def verify_key_record(self, raw_key: str) -> dict:
        token = (raw_key or "").strip()
        if not token.startswith(KEY_PREFIX) or len(token) < len(KEY_PREFIX) + 16:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key.",
            )
        digest = hash_api_key(token)
        record = self.repository.get_by_key_hash(digest)
        if not record or not hmac.compare_digest(record["key_hash"], digest):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key.",
            )
        if record.get("void") or not record.get("is_active"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key.",
            )
        expires_at = _as_utc(record.get("expires_at"))
        if expires_at is not None and expires_at <= _utcnow():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="This API key has expired.",
            )
        self.repository.touch_last_used(int(record["id"]))
        return record

    def authenticate_principal(self, raw_key: str) -> AdminResponse:
        record = self.verify_key_record(raw_key)
        scopes = ordered(c for c in record.get("permissions", []) if c in APP_ALLOWED_SCOPES)
        return AdminResponse(
            id=int(record["created_by"]),
            username=f"app:{record['key_prefix']}",
            is_active=True,
            permissions=scopes,
        )

    def identify(self, raw_key: str) -> AppIdentityResponse:
        record = self.verify_key_record(raw_key)
        scopes = ordered(c for c in record.get("permissions", []) if c in APP_ALLOWED_SCOPES)
        return AppIdentityResponse(
            id=int(record["id"]),
            name=record["name"],
            key_prefix=record["key_prefix"],
            permissions=scopes,
            expires_at=record.get("expires_at"),
        )
