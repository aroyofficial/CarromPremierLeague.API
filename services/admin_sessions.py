"""One live admin sign-in, plus a request the current sign-in must approve."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

REQUEST_MINUTES = 10
IDLE_MINUTES = 5


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _idle(seen_at: datetime | None, now: datetime) -> bool:
    if seen_at is None:
        return False
    return seen_at + timedelta(minutes=IDLE_MINUTES) <= now


@dataclass
class LiveSession:
    admin_id: int
    device_id: str
    device_label: str
    expires_at: datetime
    seen_at: datetime | None = None


@dataclass
class LoginChallenge:
    id: str
    admin_id: int
    device_id: str
    device_label: str
    status: str
    expires_at: datetime
    access_token: str | None = None


def _label(value: str | None) -> str:
    text = (value or "").strip()
    return (text or "Another device")[:160]


class MemorySessionStore:
    def __init__(self):
        self.sessions: dict[int, LiveSession] = {}
        self.requests: dict[str, LoginChallenge] = {}

    def drop_if_stale(self, admin_id: int, now: datetime) -> bool:
        current = self.sessions.get(admin_id)
        if current is None or not (current.expires_at <= now or _idle(current.seen_at, now)):
            return False
        self.sessions.pop(admin_id, None)
        return True

    def touch(self, admin_id: int, now: datetime) -> None:
        current = self.sessions.get(admin_id)
        if current is None or current.expires_at <= now or _idle(current.seen_at, now):
            return
        current.seen_at = now

    def pending_request(self, request_id: str, now: datetime) -> LoginChallenge | None:
        challenge = self.requests.get(request_id)
        if challenge is None or challenge.status != "pending" or challenge.expires_at <= now:
            return None
        return challenge

    def begin(self, admin_id: int, device_id: str, device_label: str | None, now: datetime) -> LoginChallenge | None:
        self.drop_if_stale(admin_id, now)
        current = self.sessions.get(admin_id)
        if current is None or current.device_id == device_id:
            return None
        for item in list(self.requests.values()):
            if item.admin_id == admin_id and item.status == "pending":
                item.status = "expired"
        challenge = LoginChallenge(
            id=secrets.token_urlsafe(24),
            admin_id=admin_id,
            device_id=device_id,
            device_label=_label(device_label),
            status="pending",
            expires_at=now + timedelta(minutes=REQUEST_MINUTES),
        )
        self.requests[challenge.id] = challenge
        return challenge

    def activate(
        self, admin_id: int, device_id: str, device_label: str | None, expires_at: datetime, seen_at: datetime | None = None
    ) -> None:
        self.sessions[admin_id] = LiveSession(admin_id, device_id, _label(device_label), expires_at, seen_at or _now())

    def live(self, admin_id: int, now: datetime) -> LiveSession | None:
        current = self.sessions.get(admin_id)
        if current is None or current.expires_at <= now or _idle(current.seen_at, now):
            return None
        return current

    def clear(self, admin_id: int) -> None:
        self.sessions.pop(admin_id, None)

    def pending_for(self, admin_id: int, now: datetime) -> LoginChallenge | None:
        found = [
            item
            for item in self.requests.values()
            if item.admin_id == admin_id and item.status == "pending" and item.expires_at > now
        ]
        return found[-1] if found else None

    def get(self, request_id: str) -> LoginChallenge | None:
        return self.requests.get(request_id)

    def require_pending(self, admin_id: int, request_id: str, now: datetime) -> LoginChallenge:
        return self._open(admin_id, request_id, now)

    def resolve(self, admin_id: int, request_id: str, status: str, now: datetime, token: str | None = None) -> LoginChallenge:
        challenge = self._open(admin_id, request_id, now)
        challenge.status = status
        challenge.access_token = token
        return challenge

    def claim(self, request_id: str, now: datetime) -> LoginChallenge | None:
        challenge = self.requests.get(request_id)
        if challenge is None:
            return None
        if challenge.status == "pending" and challenge.expires_at <= now:
            challenge.status = "expired"
        if challenge.status == "approved" and challenge.access_token:
            token = challenge.access_token
            challenge.status = "claimed"
            challenge.access_token = None
            return LoginChallenge(
                challenge.id,
                challenge.admin_id,
                challenge.device_id,
                challenge.device_label,
                "approved",
                challenge.expires_at,
                token,
            )
        return challenge

    def _open(self, admin_id: int, request_id: str, now: datetime) -> LoginChallenge:
        from fastapi import HTTPException

        challenge = self.requests.get(request_id)
        if challenge is None or challenge.admin_id != admin_id:
            raise HTTPException(status_code=404, detail="That sign-in request was not found.")
        if challenge.status != "pending" or challenge.expires_at <= now:
            raise HTTPException(status_code=409, detail="That sign-in request is no longer waiting.")
        return challenge


class SqlSessionStore:
    def __init__(self, db):
        self.db = db

    def drop_if_stale(self, admin_id: int, now: datetime) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT ExpiresAt, LastSeenAt
            FROM tblAdminSessions
            WHERE AdminId = %s
            """,
            (admin_id,),
        )
        current = cursor.fetchone()
        if current is None or not (current["ExpiresAt"] <= now or _idle(current["LastSeenAt"], now)):
            return False
        cursor.execute("DELETE FROM tblAdminSessions WHERE AdminId = %s", (admin_id,))
        self.db.commit()
        return True

    def touch(self, admin_id: int, now: datetime) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblAdminSessions
            SET LastSeenAt = %s
            WHERE AdminId = %s AND ExpiresAt > %s AND (LastSeenAt IS NULL OR LastSeenAt > %s)
            """,
            (now, admin_id, now, now - timedelta(minutes=IDLE_MINUTES)),
        )
        self.db.commit()

    def pending_request(self, request_id: str, now: datetime) -> LoginChallenge | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, AdminId, DeviceId, DeviceLabel, Status, ExpiresAt
            FROM tblAdminLoginRequests
            WHERE Id = %s AND Status = 'pending' AND ExpiresAt > %s
            """,
            (request_id, now),
        )
        return self._row(cursor.fetchone())

    def begin(self, admin_id: int, device_id: str, device_label: str | None, now: datetime) -> LoginChallenge | None:
        if self.drop_if_stale(admin_id, now):
            return None
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT DeviceId
            FROM tblAdminSessions
            WHERE AdminId = %s AND ExpiresAt > %s
            """,
            (admin_id, now),
        )
        current = cursor.fetchone()
        if current is None or current["DeviceId"] == device_id:
            return None
        cursor.execute(
            """
            UPDATE tblAdminLoginRequests
            SET Status = 'expired'
            WHERE AdminId = %s AND Status = 'pending'
            """,
            (admin_id,),
        )
        challenge = LoginChallenge(
            id=secrets.token_urlsafe(24),
            admin_id=admin_id,
            device_id=device_id,
            device_label=_label(device_label),
            status="pending",
            expires_at=now + timedelta(minutes=REQUEST_MINUTES),
        )
        cursor.execute(
            """
            INSERT INTO tblAdminLoginRequests (Id, AdminId, DeviceId, DeviceLabel, Status, ExpiresAt)
            VALUES (%s, %s, %s, %s, 'pending', %s)
            """,
            (challenge.id, admin_id, device_id, challenge.device_label, challenge.expires_at),
        )
        self.db.commit()
        return challenge

    def activate(
        self, admin_id: int, device_id: str, device_label: str | None, expires_at: datetime, seen_at: datetime | None = None
    ) -> None:
        seen = seen_at or _now()
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM tblAdminSessions WHERE AdminId = %s", (admin_id,))
        cursor.execute(
            """
            INSERT INTO tblAdminSessions (AdminId, DeviceId, DeviceLabel, ExpiresAt, LastSeenAt)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (admin_id, device_id, _label(device_label), expires_at, seen),
        )
        self.db.commit()

    def live(self, admin_id: int, now: datetime) -> LiveSession | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT AdminId, DeviceId, DeviceLabel, ExpiresAt, LastSeenAt
            FROM tblAdminSessions
            WHERE AdminId = %s AND ExpiresAt > %s
            """,
            (admin_id, now),
        )
        row = cursor.fetchone()
        if row is None or _idle(row["LastSeenAt"], now):
            return None
        return LiveSession(int(row["AdminId"]), row["DeviceId"], row["DeviceLabel"], row["ExpiresAt"], row["LastSeenAt"])

    def clear(self, admin_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM tblAdminSessions WHERE AdminId = %s", (admin_id,))
        self.db.commit()

    def pending_for(self, admin_id: int, now: datetime) -> LoginChallenge | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT TOP (1) Id, AdminId, DeviceId, DeviceLabel, Status, ExpiresAt
            FROM tblAdminLoginRequests
            WHERE AdminId = %s AND Status = 'pending' AND ExpiresAt > %s
            ORDER BY CreatedAt DESC
            """,
            (admin_id, now),
        )
        return self._row(cursor.fetchone())

    def require_pending(self, admin_id: int, request_id: str, now: datetime) -> LoginChallenge:
        from fastapi import HTTPException

        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, AdminId, DeviceId, DeviceLabel, ExpiresAt
            FROM tblAdminLoginRequests
            WHERE Id = %s AND AdminId = %s AND Status = 'pending' AND ExpiresAt > %s
            """,
            (request_id, admin_id, now),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="That sign-in request is no longer waiting.")
        return LoginChallenge(
            row["Id"], int(row["AdminId"]), row["DeviceId"], row["DeviceLabel"], "pending", row["ExpiresAt"]
        )

    def resolve(self, admin_id: int, request_id: str, status: str, now: datetime, token: str | None = None) -> LoginChallenge:
        from fastapi import HTTPException

        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, AdminId, DeviceId, DeviceLabel, ExpiresAt
            FROM tblAdminLoginRequests
            WHERE Id = %s AND AdminId = %s AND Status = 'pending' AND ExpiresAt > %s
            """,
            (request_id, admin_id, now),
        )
        row = cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=409, detail="That sign-in request is no longer waiting.")
        cursor.execute(
            """
            UPDATE tblAdminLoginRequests
            SET Status = %s, AccessToken = %s
            WHERE Id = %s AND Status = 'pending'
            """,
            (status, token, request_id),
        )
        self.db.commit()
        return LoginChallenge(
            row["Id"],
            int(row["AdminId"]),
            row["DeviceId"],
            row["DeviceLabel"],
            status,
            row["ExpiresAt"],
            token,
        )

    def claim(self, request_id: str, now: datetime) -> LoginChallenge | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, AdminId, DeviceId, DeviceLabel, Status, AccessToken, ExpiresAt
            FROM tblAdminLoginRequests
            WHERE Id = %s
            """,
            (request_id,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if row["Status"] == "pending" and row["ExpiresAt"] <= now:
            cursor.execute(
                "UPDATE tblAdminLoginRequests SET Status = 'expired' WHERE Id = %s AND Status = 'pending'",
                (request_id,),
            )
            self.db.commit()
            row["Status"] = "expired"
        challenge = self._row(row)
        if row["Status"] == "approved" and row["AccessToken"]:
            cursor.execute(
                """
                UPDATE tblAdminLoginRequests
                SET Status = 'claimed', AccessToken = NULL
                WHERE Id = %s AND Status = 'approved'
                """,
                (request_id,),
            )
            self.db.commit()
            challenge.access_token = row["AccessToken"]
            challenge.status = "approved"
        return challenge

    @staticmethod
    def _row(row) -> LoginChallenge | None:
        if row is None:
            return None
        return LoginChallenge(
            id=row["Id"],
            admin_id=int(row["AdminId"]),
            device_id=row["DeviceId"],
            device_label=row["DeviceLabel"],
            status=row["Status"],
            expires_at=row["ExpiresAt"],
            access_token=row.get("AccessToken") if isinstance(row, dict) else None,
        )
