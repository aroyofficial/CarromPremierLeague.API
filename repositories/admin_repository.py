from datetime import date, datetime

from schemas.auth_schema import AdminResponse

_ADMIN_COLUMNS = """
    Id, Username, IsActive, MustChangePassword, FirstName, LastName, DateOfBirth, Gender,
    PhoneCode, PhoneNumber, Email, AvatarUrl, PasswordChangedAt
"""


class AdminRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _as_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return value

    @staticmethod
    def _to_response(row: dict) -> AdminResponse:
        return AdminResponse(
            id=row["Id"],
            username=row["Username"],
            is_active=bool(row["IsActive"]),
            first_name=row.get("FirstName"),
            last_name=row.get("LastName"),
            date_of_birth=AdminRepository._as_date(row.get("DateOfBirth")),
            gender=row.get("Gender"),
            phone_code=row.get("PhoneCode"),
            phone_number=row.get("PhoneNumber"),
            email=row.get("Email"),
            avatar_url=row.get("AvatarUrl"),
            must_change_password=bool(row.get("MustChangePassword")),
            password_changed_at=row.get("PasswordChangedAt"),
        )

    def count_active(self) -> int:
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS Count FROM tblAdmins WHERE Void = 0 AND IsActive = 1")
        return int(cursor.fetchone()["Count"])

    def count_all(self, lock: bool = False) -> int:
        cursor = self.db.cursor()
        lock_hint = " WITH (UPDLOCK, HOLDLOCK)" if lock else ""
        cursor.execute(f"SELECT COUNT(*) AS Count FROM tblAdmins{lock_hint}")
        return int(cursor.fetchone()["Count"])

    def get_credentials_by_username(self, username: str) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Username, PasswordHash, PasswordSalt, PasswordIterations, TokenVersion, IsActive,
                   MustChangePassword, FirstName, LastName, DateOfBirth, Gender, PhoneCode, PhoneNumber, Email, AvatarUrl
            FROM tblAdmins
            WHERE Username = %s AND Void = 0
            """,
            (username,),
        )
        return cursor.fetchone()

    def find_email(self, email: str) -> int | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT TOP (1) Id
            FROM tblAdmins
            WHERE Email = %s AND Void = 0
            """,
            (email,),
        )
        row = cursor.fetchone()
        return int(row["Id"]) if row else None

    def get_by_id(self, admin_id: int) -> AdminResponse | None:
        cursor = self.db.cursor()
        cursor.execute(
            f"""
            SELECT {_ADMIN_COLUMNS}
            FROM tblAdmins
            WHERE Id = %s AND Void = 0
            """,
            (admin_id,),
        )
        row = cursor.fetchone()
        return self._to_response(row) if row else None

    def get_all(self) -> list[AdminResponse]:
        cursor = self.db.cursor()
        cursor.execute(
            f"""
            SELECT {_ADMIN_COLUMNS}
            FROM tblAdmins
            WHERE Void = 0
            ORDER BY Username
            """
        )
        return [self._to_response(row) for row in cursor.fetchall()]

    def create(
        self,
        username: str,
        password_hash: str,
        password_salt: str,
        password_iterations: int,
        profile: dict | None = None,
        is_active: bool = True,
        must_change_password: bool = False,
    ) -> AdminResponse:
        profile = profile or {}
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblAdmins (
                Username, PasswordHash, PasswordSalt, PasswordIterations, IsActive, MustChangePassword,
                FirstName, LastName, DateOfBirth, Gender, PhoneCode, PhoneNumber, Email
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                username,
                password_hash,
                password_salt,
                password_iterations,
                int(is_active),
                int(must_change_password),
                profile.get("first_name"),
                profile.get("last_name"),
                profile.get("date_of_birth"),
                profile.get("gender"),
                profile.get("phone_code"),
                profile.get("phone_number"),
                profile.get("email"),
            ),
        )
        self.db.commit()
        return self.get_by_id(cursor.lastrowid)

    def update(self, admin_id: int, values: dict) -> AdminResponse | None:
        if not values:
            return self.get_by_id(admin_id)
        mapping = {
            "username": "Username",
            "password_hash": "PasswordHash",
            "password_salt": "PasswordSalt",
            "password_iterations": "PasswordIterations",
            "is_active": "IsActive",
            "must_change_password": "MustChangePassword",
            "first_name": "FirstName",
            "last_name": "LastName",
            "date_of_birth": "DateOfBirth",
            "gender": "Gender",
            "phone_code": "PhoneCode",
            "phone_number": "PhoneNumber",
            "email": "Email",
            "password_changed_at": "PasswordChangedAt",
        }
        increment_token_version = values.pop("increment_token_version", False)
        fields = [f"{mapping[key]} = %s" for key in values]
        if increment_token_version:
            fields.append("TokenVersion = TokenVersion + 1")
        cursor = self.db.cursor()
        cursor.execute(
            f"""
            UPDATE tblAdmins
            SET {", ".join(fields)}, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Void = 0
            """,
            tuple(values.values()) + (admin_id,),
        )
        self.db.commit()
        return self.get_by_id(admin_id)

    def delete(self, admin_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblAdmins
            SET Void = 1, IsActive = 0, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Void = 0
            """,
            (admin_id,),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def get_avatar_key(self, admin_id: int) -> tuple[bool, str | None]:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT AvatarStorageKey
            FROM tblAdmins
            WHERE Id = %s AND Void = 0
            """,
            (admin_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False, None
        return True, row["AvatarStorageKey"]

    def set_avatar(self, admin_id: int, url: str | None, key: str | None) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblAdmins
            SET AvatarUrl = %s, AvatarStorageKey = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Void = 0
            """,
            (url, key, admin_id),
        )
        self.db.commit()
        return cursor.rowcount > 0
