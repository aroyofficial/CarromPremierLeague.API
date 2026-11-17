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
# A verified email code is exchanged for a ticket, which then allows exactly one password change or reset.
TICKET_PURPOSE = "password_ticket"

# Outcomes of checking a code. Only CODE_OK lets the action continue.
CODE_OK = "ok"
CODE_INCORRECT = "incorrect"
CODE_EXPIRED = "expired"
CODE_USED = "used"
CODE_LIMITED = "limited"
CODE_MISSING = "missing"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _new_secret(purpose: str) -> str:
    if purpose == TICKET_PURPOSE:
        return secrets.token_urlsafe(32)
    return f"{secrets.randbelow(1000000):06d}"


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
        code = _new_secret(purpose)
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

    def check(self, admin_id: int, purpose: str, destination: str, code: str) -> str:
        row = self._latest(admin_id, purpose)
        if row is None or row.destination != (destination or "").lower():
            return CODE_MISSING
        if row.consumed:
            return CODE_USED
        if row.expires_at <= _utcnow():
            return CODE_EXPIRED
        if row.attempts >= MAX_ATTEMPTS:
            return CODE_LIMITED
        if not hmac.compare_digest(row.code_hash, _hash(code or "", row.code_salt)):
            row.attempts += 1
            return CODE_LIMITED if row.attempts >= MAX_ATTEMPTS else CODE_INCORRECT
        row.consumed = True
        return CODE_OK

    def verify(self, admin_id: int, purpose: str, destination: str, code: str) -> bool:
        return self.check(admin_id, purpose, destination, code) == CODE_OK

    def _latest(self, admin_id: int, purpose: str) -> StoredCode | None:
        matches = [row for row in self.rows if row.admin_id == admin_id and row.purpose == purpose]
        return matches[-1] if matches else None


class SqlCodeStore:
    def __init__(self, db):
        self.db = db

    def issue(self, admin_id: int, purpose: str, destination: str) -> str:
        code = _new_secret(purpose)
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

    def check(self, admin_id: int, purpose: str, destination: str, code: str) -> str:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT TOP (1) Id, Destination, CodeHash, CodeSalt, Attempts, ConsumedAt,
                   CASE WHEN ExpiresAt <= SYSUTCDATETIME() THEN 1 ELSE 0 END AS Expired
            FROM tblAdminCodes
            WHERE AdminId = %s AND Purpose = %s
            ORDER BY Id DESC
            """,
            (admin_id, purpose),
        )
        row = cursor.fetchone()
        if not row or (row["Destination"] or "").lower() != (destination or "").lower():
            return CODE_MISSING
        if row["ConsumedAt"] is not None:
            return CODE_USED
        if int(row["Expired"]) == 1:
            return CODE_EXPIRED
        attempts = int(row["Attempts"])
        if attempts >= MAX_ATTEMPTS:
            return CODE_LIMITED
        # Both updates are conditional, so parallel requests cannot use one code twice or push the
        # attempts past the limit.
        if not hmac.compare_digest(row["CodeHash"], _hash(code or "", row["CodeSalt"])):
            cursor.execute(
                "UPDATE tblAdminCodes SET Attempts = Attempts + 1 WHERE Id = %s AND ConsumedAt IS NULL AND Attempts < %s",
                (row["Id"], MAX_ATTEMPTS),
            )
            counted = cursor.rowcount == 1
            self.db.commit()
            return CODE_LIMITED if not counted or attempts + 1 >= MAX_ATTEMPTS else CODE_INCORRECT
        return CODE_OK if self._consume(row["Id"]) else CODE_USED

    def verify(self, admin_id: int, purpose: str, destination: str, code: str) -> bool:
        return self.check(admin_id, purpose, destination, code) == CODE_OK

    def _consume(self, code_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblAdminCodes SET ConsumedAt = SYSUTCDATETIME()
            WHERE Id = %s AND ConsumedAt IS NULL AND Attempts < %s AND ExpiresAt > SYSUTCDATETIME()
            """,
            (code_id, MAX_ATTEMPTS),
        )
        consumed = cursor.rowcount == 1
        self.db.commit()
        return consumed
