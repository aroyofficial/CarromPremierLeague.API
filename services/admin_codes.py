"""Short-lived codes for confirming an email address or resetting a password."""

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from core.config import settings

CODE_MINUTES = 10
MAX_ATTEMPTS = 5
MAX_PER_HOUR = 5


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _hash(code: str, salt: str) -> str:
    return hmac.new(settings.AUTH_JWT_SECRET.encode(), f"{salt}:{code}".encode(), sha256).hexdigest()


@dataclass
class StoredCode:
    id: int
    admin_id: int
    purpose: str
    destination: str
    code_hash: str
    code_salt: str
    attempts: int
    expires_at: datetime
    consumed: bool


class MemoryCodeStore:
    def __init__(self):
        self.rows: list[StoredCode] = []
        self._next = 1

    def issue(self, admin_id: int, purpose: str, destination: str) -> str:
        code = f"{secrets.randbelow(1000000):06d}"
        salt = secrets.token_hex(16)
        self.rows.append(StoredCode(
            id=self._next,
            admin_id=admin_id,
            purpose=purpose,
            destination=destination.lower(),
            code_hash=_hash(code, salt),
            code_salt=salt,
            attempts=0,
            expires_at=_utcnow() + timedelta(minutes=CODE_MINUTES),
            consumed=False,
        ))
        self._next += 1
        return code

    def count_since(self, admin_id: int, purpose: str, since: datetime) -> int:
        return sum(
            1 for row in self.rows
            if row.admin_id == admin_id and row.purpose == purpose and row.expires_at - timedelta(minutes=CODE_MINUTES) >= since
        )

    def verify(self, admin_id: int, purpose: str, destination: str, code: str) -> bool:
        row = self._open(admin_id, purpose)
        if row is None or row.destination != (destination or "").lower():
            return False
        if row.attempts >= MAX_ATTEMPTS:
            row.consumed = True
            return False
        if not hmac.compare_digest(row.code_hash, _hash(code or "", row.code_salt)):
            row.attempts += 1
            return False
        row.consumed = True
        return True

    def _open(self, admin_id: int, purpose: str) -> StoredCode | None:
        now = _utcnow()
        open_rows = [
            row for row in self.rows
            if row.admin_id == admin_id and row.purpose == purpose and not row.consumed and row.expires_at > now
        ]
        return open_rows[-1] if open_rows else None


class SqlCodeStore:
    def __init__(self, db):
        self.db = db

    def issue(self, admin_id: int, purpose: str, destination: str) -> str:
        code = f"{secrets.randbelow(1000000):06d}"
        salt = secrets.token_hex(16)
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblAdminCodes (AdminId, Purpose, Destination, CodeHash, CodeSalt, ExpiresAt)
            VALUES (%s, %s, %s, %s, %s, DATEADD(minute, %s, SYSUTCDATETIME()))
            """,
            (admin_id, purpose, destination.lower(), _hash(code, salt), salt, CODE_MINUTES),
        )
        self.db.commit()
        return code

    def count_since(self, admin_id: int, purpose: str, since: datetime) -> int:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS Count
            FROM tblAdminCodes
            WHERE AdminId = %s AND Purpose = %s AND CreatedAt >= %s
            """,
            (admin_id, purpose, since),
        )
        return int(cursor.fetchone()["Count"])

    def verify(self, admin_id: int, purpose: str, destination: str, code: str) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT TOP (1) Id, Destination, CodeHash, CodeSalt, Attempts
            FROM tblAdminCodes
            WHERE AdminId = %s AND Purpose = %s AND ConsumedAt IS NULL AND ExpiresAt > SYSUTCDATETIME()
            ORDER BY Id DESC
            """,
            (admin_id, purpose),
        )
        row = cursor.fetchone()
        if not row or (row["Destination"] or "").lower() != (destination or "").lower():
            return False
        if int(row["Attempts"]) >= MAX_ATTEMPTS:
            self._consume(row["Id"])
            return False
        if not hmac.compare_digest(row["CodeHash"], _hash(code or "", row["CodeSalt"])):
            cursor.execute(
                "UPDATE tblAdminCodes SET Attempts = Attempts + 1 WHERE Id = %s",
                (row["Id"],),
            )
            self.db.commit()
            return False
        self._consume(row["Id"])
        return True

    def _consume(self, code_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE tblAdminCodes SET ConsumedAt = SYSUTCDATETIME() WHERE Id = %s",
            (code_id,),
        )
        self.db.commit()
