"""Phone-number verification codes (Indian +91 mobiles only).

The sender is a provider behind a flag, CPL_PHONE_OTP_PROVIDER:

* "off" (default): no code is sent. Requests return 503, so nothing is trusted.
* "dev": only in APP_ENV=dev. Nothing leaves the process. The last code is kept in
  memory for tests and printed to the server console so a developer can enter it. It is never
  written to the application log or to a response.

No production SMS provider is configured in this project, so production SMS is not enabled.

Codes are stored as HMAC hashes with a per-code salt. They expire, allow a limited number of
wrong attempts, and cannot be requested again within the cooldown or the hourly cap.
"""

import hmac
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from fastapi import HTTPException, status

from core.config import settings

CODE_MINUTES = 10
MAX_ATTEMPTS = 5
COOLDOWN_SECONDS = 60
MAX_PER_HOUR = 5
COUNTRY = "+91"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(code: str, salt: str) -> str:
    return hmac.new(settings.AUTH_JWT_SECRET.encode(), f"phone:{salt}:{code}".encode(), sha256).hexdigest()


class PhoneOtpUnavailable(Exception):
    pass


class DisabledPhoneSender:
    name = "off"
    configured = False

    def send(self, e164: str, code: str) -> None:
        raise PhoneOtpUnavailable()


class DevPhoneSender:
    """Development only. Never transmits.

    Keeps the last code per number for tests, and prints it to the server console (not the log)
    so a developer can finish the flow by hand. sender_from_settings returns it only when
    APP_ENV is dev, so no other environment ever prints a code.
    """

    name = "dev"
    configured = True

    def __init__(self, echo: bool = False):
        self.last_code: dict[str, str] = {}
        self.echo = echo

    def send(self, e164: str, code: str) -> None:
        self.last_code[e164] = code
        if self.echo:
            print(f"[dev phone code] ...{e164[-4:]}: {code}", file=sys.stderr, flush=True)


_dev_sender: DevPhoneSender | None = None


def sender_from_settings():
    global _dev_sender
    if settings.PHONE_OTP_PROVIDER == "dev" and settings.APP_ENV == "dev":
        # One sender per process, so the code survives from the request call to the confirm call.
        if _dev_sender is None:
            _dev_sender = DevPhoneSender(echo=True)
        return _dev_sender
    return DisabledPhoneSender()


@dataclass
class StoredPhoneCode:
    id: int
    code_hash: str
    code_salt: str
    attempts: int
    expires_at: datetime
    consumed: bool


class MemoryPhoneOtpStore:
    def __init__(self):
        self.rows: list[dict] = []
        self._next = 1

    def issue(self, kind: str, owner_id: int, e164: str, code_hash: str, salt: str, expires_at: datetime) -> None:
        self.rows.append({
            "id": self._next, "kind": kind, "owner": owner_id, "e164": e164, "hash": code_hash,
            "salt": salt, "attempts": 0, "expires_at": expires_at, "consumed": False, "at": _utcnow(),
        })
        self._next += 1

    def latest(self, kind: str, owner_id: int, e164: str) -> StoredPhoneCode | None:
        matches = [r for r in self.rows if r["kind"] == kind and r["owner"] == owner_id and r["e164"] == e164]
        if not matches:
            return None
        row = matches[-1]
        return StoredPhoneCode(row["id"], row["hash"], row["salt"], row["attempts"], row["expires_at"], row["consumed"])

    def last_issued_at(self, kind: str, owner_id: int) -> datetime | None:
        times = [r["at"] for r in self.rows if r["kind"] == kind and r["owner"] == owner_id]
        return max(times) if times else None

    def count_since(self, kind: str, owner_id: int, since: datetime) -> int:
        return sum(1 for r in self.rows if r["kind"] == kind and r["owner"] == owner_id and r["at"] >= since)

    def add_attempt(self, code_id: int) -> bool:
        for r in self.rows:
            if r["id"] == code_id and not r["consumed"] and r["attempts"] < MAX_ATTEMPTS:
                r["attempts"] += 1
                return True
        return False

    def consume(self, code_id: int) -> bool:
        for r in self.rows:
            if (r["id"] == code_id and not r["consumed"] and r["attempts"] < MAX_ATTEMPTS
                    and r["expires_at"] > _utcnow()):
                r["consumed"] = True
                return True
        return False


class SqlPhoneOtpStore:
    def __init__(self, db):
        self.db = db

    def issue(self, kind: str, owner_id: int, e164: str, code_hash: str, salt: str, expires_at: datetime) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblPhoneOtps (OwnerKind, OwnerId, PhoneE164, CodeHash, CodeSalt, ExpiresAt, CreatedAt)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (kind, owner_id, e164, code_hash, salt, expires_at, _utcnow()),
        )
        self.db.commit()

    def latest(self, kind: str, owner_id: int, e164: str) -> StoredPhoneCode | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT TOP (1) Id, CodeHash, CodeSalt, Attempts, ExpiresAt, ConsumedAt
            FROM tblPhoneOtps
            WHERE OwnerKind = %s AND OwnerId = %s AND PhoneE164 = %s
            ORDER BY Id DESC
            """,
            (kind, owner_id, e164),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return StoredPhoneCode(
            id=int(row["Id"]),
            code_hash=row["CodeHash"],
            code_salt=row["CodeSalt"],
            attempts=int(row["Attempts"]),
            expires_at=row["ExpiresAt"],
            consumed=row["ConsumedAt"] is not None,
        )

    def last_issued_at(self, kind: str, owner_id: int) -> datetime | None:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT MAX(CreatedAt) AS At FROM tblPhoneOtps WHERE OwnerKind = %s AND OwnerId = %s",
            (kind, owner_id),
        )
        row = cursor.fetchone()
        return row["At"] if row else None

    def count_since(self, kind: str, owner_id: int, since: datetime) -> int:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS Count FROM tblPhoneOtps WHERE OwnerKind = %s AND OwnerId = %s AND CreatedAt >= %s",
            (kind, owner_id, since),
        )
        return int(cursor.fetchone()["Count"])

    # Both updates are conditional, so two parallel requests cannot both use one code or
    # push the attempts past the limit. Each returns whether it changed the row.
    def add_attempt(self, code_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE tblPhoneOtps SET Attempts = Attempts + 1 WHERE Id = %s AND ConsumedAt IS NULL AND Attempts < %s",
            (code_id, MAX_ATTEMPTS),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def consume(self, code_id: int) -> bool:
        now = _utcnow()
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblPhoneOtps SET ConsumedAt = %s
            WHERE Id = %s AND ConsumedAt IS NULL AND Attempts < %s AND ExpiresAt > %s
            """,
            (now, code_id, MAX_ATTEMPTS, now),
        )
        self.db.commit()
        return cursor.rowcount == 1


def is_india(phone_code: str) -> bool:
    return phone_code == COUNTRY


class PhoneOtpService:
    def __init__(self, store=None, sender=None):
        self.store = store if store is not None else MemoryPhoneOtpStore()
        self.sender = sender if sender is not None else DisabledPhoneSender()

    def check_request_allowed(self, kind: str, owner_id: int, phone_code: str) -> None:
        """Every check that does not look at who owns the number: country, provider, cooldown, hourly cap."""
        if not is_india(phone_code):
            raise HTTPException(status_code=400, detail="Phone verification is available for Indian (+91) mobile numbers.")
        if not self.sender.configured:
            raise HTTPException(status_code=503, detail="Phone verification is not turned on for this server.")
        now = _utcnow()
        last = self.store.last_issued_at(kind, owner_id)
        if last and (now - last).total_seconds() < COOLDOWN_SECONDS:
            raise HTTPException(
                status_code=429,
                detail="Wait a minute before requesting another code.",
                headers={"Retry-After": str(COOLDOWN_SECONDS)},
            )
        if self.store.count_since(kind, owner_id, now - timedelta(hours=1)) >= MAX_PER_HOUR:
            raise HTTPException(status_code=429, detail="Too many codes were requested. Try again later.")

    def record_refused(self, kind: str, owner_id: int, e164: str) -> None:
        """A refused request (the number belongs to someone else) still counts toward the limits.

        The stored hash is of a random secret nobody knows, and it expires at once, so it can never verify.
        """
        salt = secrets.token_hex(16)
        self.store.issue(kind, owner_id, e164, _hash(secrets.token_hex(16), salt), salt, _utcnow())

    def request(self, kind: str, owner_id: int, phone_code: str, e164: str) -> None:
        self.check_request_allowed(kind, owner_id, phone_code)
        now = _utcnow()
        code = f"{secrets.randbelow(1000000):06d}"
        salt = secrets.token_hex(16)
        self.store.issue(kind, owner_id, e164, _hash(code, salt), salt, now + timedelta(minutes=CODE_MINUTES))
        self.sender.send(e164, code)

    def verify(self, kind: str, owner_id: int, e164: str, code: str) -> None:
        """Raise a specific error for each failure state, or return when the code is correct."""
        row = self.store.latest(kind, owner_id, e164)
        if row is None:
            raise HTTPException(status_code=400, detail="Request a code first.")
        if row.consumed:
            raise HTTPException(status_code=400, detail="That code was already used. Request a new one.")
        if row.expires_at <= _utcnow():
            raise HTTPException(status_code=400, detail="That code has expired. Request a new one.")
        if row.attempts >= MAX_ATTEMPTS:
            raise HTTPException(status_code=429, detail="Too many wrong codes. Request a new one.")
        if not hmac.compare_digest(row.code_hash, _hash(code or "", row.code_salt)):
            counted = self.store.add_attempt(row.id)
            if not counted or row.attempts + 1 >= MAX_ATTEMPTS:
                raise HTTPException(status_code=429, detail="Too many wrong codes. Request a new one.")
            raise HTTPException(status_code=400, detail="That code is not right. Check it and try again.")
        if not self.store.consume(row.id):
            current = self.store.latest(kind, owner_id, e164) or row
            if current.attempts >= MAX_ATTEMPTS:
                raise HTTPException(status_code=429, detail="Too many wrong codes. Request a new one.")
            if current.expires_at <= _utcnow():
                raise HTTPException(status_code=400, detail="That code has expired. Request a new one.")
            raise HTTPException(status_code=400, detail="That code was already used. Request a new one.")
