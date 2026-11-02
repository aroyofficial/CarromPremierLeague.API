from schemas.auth_schema import AdminResponse


class AdminRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _to_response(row: dict) -> AdminResponse:
        return AdminResponse(
            id=row["Id"],
            username=row["Username"],
            is_active=bool(row["IsActive"]),
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
            SELECT Id, Username, PasswordHash, PasswordSalt, PasswordIterations, TokenVersion, IsActive
            FROM tblAdmins
            WHERE Username = %s AND Void = 0
            """,
            (username,),
        )
        return cursor.fetchone()

    def get_by_id(self, admin_id: int) -> AdminResponse | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Username, IsActive
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
            """
            SELECT Id, Username, IsActive
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
    ) -> AdminResponse:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblAdmins (Username, PasswordHash, PasswordSalt, PasswordIterations)
            VALUES (%s, %s, %s, %s)
            """,
            (username, password_hash, password_salt, password_iterations),
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
