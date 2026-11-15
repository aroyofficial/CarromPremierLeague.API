"""Storage for second sign-in factors: email codes, authenticator apps (TOTP) and passkeys."""

import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from services.admin_codes import _utcnow

CHALLENGE_MINUTES = 10
MAX_CHALLENGE_ATTEMPTS = 5


@dataclass
class MfaSettings:
    email: bool = False
    totp_secret: str | None = None
    totp_pending: str | None = None
    totp_last_step: int | None = None


@dataclass
class Passkey:
    id: int
    admin_id: int
    credential_id: str
    public_key: str
    sign_count: int
    label: str
    transports: str | None
    created_at: datetime
    last_used_at: datetime | None = None


@dataclass
class MfaChallenge:
    id: str
    admin_id: int
    purpose: str
    challenge: str | None
    device_id: str | None
    device_label: str | None
    attempts: int
    expires_at: datetime
    consumed: bool = False


class MemoryMfaStore:
    def __init__(self):
        self.settings_by_admin: dict[int, MfaSettings] = {}
        self.passkey_rows: list[Passkey] = []
        self.challenges: dict[str, MfaChallenge] = {}
        self._next = 1

    def settings(self, admin_id: int) -> MfaSettings:
        current = self.settings_by_admin.get(admin_id, MfaSettings())
        return MfaSettings(**vars(current))

    def update_settings(self, admin_id: int, **values) -> None:
        current = self.settings_by_admin.setdefault(admin_id, MfaSettings())
        for key, value in values.items():
            setattr(current, key, value)

    def passkeys(self, admin_id: int) -> list[Passkey]:
        return [row for row in self.passkey_rows if row.admin_id == admin_id]

    def passkey_by_credential(self, credential_id: str) -> Passkey | None:
        return next((row for row in self.passkey_rows if row.credential_id == credential_id), None)

    def add_passkey(self, admin_id: int, credential_id: str, public_key: str, sign_count: int, label: str, transports: str | None) -> Passkey:
        row = Passkey(self._next, admin_id, credential_id, public_key, sign_count, label, transports, _utcnow())
        self._next += 1
        self.passkey_rows.append(row)
        return row

    def touch_passkey(self, passkey_id: int, sign_count: int) -> None:
        for row in self.passkey_rows:
            if row.id == passkey_id:
                row.sign_count = sign_count
                row.last_used_at = _utcnow()

    def remove_passkey(self, admin_id: int, passkey_id: int) -> bool:
        before = len(self.passkey_rows)
        self.passkey_rows = [row for row in self.passkey_rows if not (row.id == passkey_id and row.admin_id == admin_id)]
        return len(self.passkey_rows) < before

    def create_challenge(self, admin_id: int, purpose: str, device_id: str | None = None, device_label: str | None = None) -> MfaChallenge:
        challenge = MfaChallenge(
            id=secrets.token_urlsafe(32),
            admin_id=admin_id,
            purpose=purpose,
            challenge=None,
            device_id=device_id,
            device_label=device_label,
            attempts=0,
            expires_at=_utcnow() + timedelta(minutes=CHALLENGE_MINUTES),
        )
        self.challenges[challenge.id] = challenge
        return challenge

    def open_challenge(self, challenge_id: str, purpose: str) -> MfaChallenge | None:
        row = self.challenges.get(challenge_id or "")
        if row is None or row.purpose != purpose or row.consumed or row.expires_at <= _utcnow():
            return None
        return MfaChallenge(**vars(row))

    def set_challenge_value(self, challenge_id: str, value: str) -> None:
        self.challenges[challenge_id].challenge = value

    def count_failure(self, challenge_id: str) -> int:
        row = self.challenges[challenge_id]
        row.attempts += 1
        if row.attempts >= MAX_CHALLENGE_ATTEMPTS:
            row.consumed = True
        return row.attempts

    def consume(self, challenge_id: str) -> None:
        self.challenges[challenge_id].consumed = True


class SqlMfaStore:
    _SETTING_COLUMNS = {
        "email": "MfaEmail",
        "totp_secret": "TotpSecret",
        "totp_pending": "TotpPendingSecret",
        "totp_last_step": "TotpLastStep",
    }

    def __init__(self, db):
        self.db = db

    def settings(self, admin_id: int) -> MfaSettings:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT MfaEmail, TotpSecret, TotpPendingSecret, TotpLastStep FROM tblAdmins WHERE Id = %s",
            (admin_id,),
        )
        row = cursor.fetchone()
        if not row:
            return MfaSettings()
        return MfaSettings(
            email=bool(row["MfaEmail"]),
            totp_secret=row["TotpSecret"],
            totp_pending=row["TotpPendingSecret"],
            totp_last_step=int(row["TotpLastStep"]) if row["TotpLastStep"] is not None else None,
        )

    def update_settings(self, admin_id: int, **values) -> None:
        if not values:
            return
        assignments = ", ".join(f"{self._SETTING_COLUMNS[key]} = %s" for key in values)
        cursor = self.db.cursor()
        cursor.execute(
            f"UPDATE tblAdmins SET {assignments}, UpdatedAt = SYSUTCDATETIME() WHERE Id = %s",
            (*values.values(), admin_id),
        )
        self.db.commit()

    @staticmethod
    def _passkey(row) -> Passkey:
        return Passkey(
            id=int(row["Id"]),
            admin_id=int(row["AdminId"]),
            credential_id=row["CredentialId"],
            public_key=row["PublicKey"],
            sign_count=int(row["SignCount"]),
            label=row["Label"],
            transports=row["Transports"],
            created_at=row["CreatedAt"],
            last_used_at=row["LastUsedAt"],
        )

    _PASSKEY_COLUMNS = "Id, AdminId, CredentialId, PublicKey, SignCount, Label, Transports, CreatedAt, LastUsedAt"

    def passkeys(self, admin_id: int) -> list[Passkey]:
        cursor = self.db.cursor()
        cursor.execute(
            f"SELECT {self._PASSKEY_COLUMNS} FROM tblAdminPasskeys WHERE AdminId = %s ORDER BY Id",
            (admin_id,),
        )
        return [self._passkey(row) for row in cursor.fetchall()]

    def passkey_by_credential(self, credential_id: str) -> Passkey | None:
        cursor = self.db.cursor()
        cursor.execute(
            f"SELECT {self._PASSKEY_COLUMNS} FROM tblAdminPasskeys WHERE CredentialId = %s",
            (credential_id,),
        )
        row = cursor.fetchone()
        return self._passkey(row) if row else None

    def add_passkey(self, admin_id: int, credential_id: str, public_key: str, sign_count: int, label: str, transports: str | None) -> Passkey:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblAdminPasskeys (AdminId, CredentialId, PublicKey, SignCount, Label, Transports)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (admin_id, credential_id, public_key, sign_count, label, transports),
        )
        self.db.commit()
        return self.passkey_by_credential(credential_id)

    def touch_passkey(self, passkey_id: int, sign_count: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE tblAdminPasskeys SET SignCount = %s, LastUsedAt = SYSUTCDATETIME() WHERE Id = %s",
            (sign_count, passkey_id),
        )
        self.db.commit()

    def remove_passkey(self, admin_id: int, passkey_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "DELETE FROM tblAdminPasskeys WHERE Id = %s AND AdminId = %s",
            (passkey_id, admin_id),
        )
        removed = cursor.rowcount > 0
        self.db.commit()
        return removed

    def create_challenge(self, admin_id: int, purpose: str, device_id: str | None = None, device_label: str | None = None) -> MfaChallenge:
        challenge = MfaChallenge(
            id=secrets.token_urlsafe(32),
            admin_id=admin_id,
            purpose=purpose,
            challenge=None,
            device_id=device_id,
            device_label=(device_label or "")[:160] or None,
            attempts=0,
            expires_at=_utcnow() + timedelta(minutes=CHALLENGE_MINUTES),
        )
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblAdminMfaChallenges (Id, AdminId, Purpose, DeviceId, DeviceLabel, ExpiresAt)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (challenge.id, admin_id, purpose, device_id, challenge.device_label, challenge.expires_at),
        )
        self.db.commit()
        return challenge

    def open_challenge(self, challenge_id: str, purpose: str) -> MfaChallenge | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, AdminId, Purpose, Challenge, DeviceId, DeviceLabel, Attempts, ExpiresAt
            FROM tblAdminMfaChallenges
            WHERE Id = %s AND Purpose = %s AND ConsumedAt IS NULL AND ExpiresAt > SYSUTCDATETIME()
            """,
            (challenge_id, purpose),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return MfaChallenge(
            id=row["Id"],
            admin_id=int(row["AdminId"]),
            purpose=row["Purpose"],
            challenge=row["Challenge"],
            device_id=row["DeviceId"],
            device_label=row["DeviceLabel"],
            attempts=int(row["Attempts"]),
            expires_at=row["ExpiresAt"],
        )

    def set_challenge_value(self, challenge_id: str, value: str) -> None:
        cursor = self.db.cursor()
        cursor.execute("UPDATE tblAdminMfaChallenges SET Challenge = %s WHERE Id = %s", (value, challenge_id))
        self.db.commit()

    def count_failure(self, challenge_id: str) -> int:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblAdminMfaChallenges
            SET Attempts = Attempts + 1,
                ConsumedAt = CASE WHEN Attempts + 1 >= %s THEN SYSUTCDATETIME() ELSE ConsumedAt END
            WHERE Id = %s
            """,
            (MAX_CHALLENGE_ATTEMPTS, challenge_id),
        )
        cursor.execute("SELECT Attempts FROM tblAdminMfaChallenges WHERE Id = %s", (challenge_id,))
        row = cursor.fetchone()
        self.db.commit()
        return int(row["Attempts"]) if row else MAX_CHALLENGE_ATTEMPTS

    def consume(self, challenge_id: str) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE tblAdminMfaChallenges SET ConsumedAt = SYSUTCDATETIME() WHERE Id = %s",
            (challenge_id,),
        )
        self.db.commit()
