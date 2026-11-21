from datetime import datetime

from core.permissions import ALL_CODES, ordered


class AppRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, (bytes, bytearray)):
            return bool(value[0]) if value else False
        return bool(value)

    def _permissions_by_app(self, app_ids: list[int]) -> dict[int, list[str]]:
        if not app_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(app_ids))
        cursor = self.db.cursor()
        cursor.execute(
            f"SELECT AppId, PermissionCode FROM tblApiAppPermissions WHERE AppId IN ({placeholders})",
            tuple(app_ids),
        )
        grouped: dict[int, set[str]] = {}
        for row in cursor.fetchall():
            code = row["PermissionCode"]
            if code in ALL_CODES:
                grouped.setdefault(int(row["AppId"]), set()).add(code)
        return {app_id: ordered(codes) for app_id, codes in grouped.items()}

    def _row_to_dict(self, row: dict, permissions: list[str]) -> dict:
        return {
            "id": int(row["Id"]),
            "name": row["Name"],
            "description": row.get("Description"),
            "key_prefix": row["KeyPrefix"],
            "key_hash": row["KeyHash"],
            "permissions": permissions,
            "is_active": self._as_bool(row["IsActive"]),
            "void": self._as_bool(row.get("Void", 0)),
            "expires_at": row.get("ExpiresAt"),
            "last_used_at": row.get("LastUsedAt"),
            "revoked_at": row.get("RevokedAt"),
            "created_at": row.get("CreatedAt"),
            "updated_at": row.get("UpdatedAt"),
            "created_by": int(row["CreatedBy"]),
        }

    def list_all(self) -> list[dict]:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Name, Description, KeyPrefix, KeyHash, IsActive, Void,
                   ExpiresAt, LastUsedAt, RevokedAt, CreatedAt, UpdatedAt, CreatedBy
            FROM tblApiApps
            WHERE Void = 0
            ORDER BY Id DESC
            """
        )
        rows = cursor.fetchall()
        perms = self._permissions_by_app([int(r["Id"]) for r in rows])
        return [self._row_to_dict(row, perms.get(int(row["Id"]), [])) for row in rows]

    def get_by_id(self, app_id: int) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Name, Description, KeyPrefix, KeyHash, IsActive, Void,
                   ExpiresAt, LastUsedAt, RevokedAt, CreatedAt, UpdatedAt, CreatedBy
            FROM tblApiApps
            WHERE Id = %s AND Void = 0
            """,
            (app_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        perms = self._permissions_by_app([int(row["Id"])])
        return self._row_to_dict(row, perms.get(int(row["Id"]), []))

    def get_by_key_hash(self, key_hash: str) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Name, Description, KeyPrefix, KeyHash, IsActive, Void,
                   ExpiresAt, LastUsedAt, RevokedAt, CreatedAt, UpdatedAt, CreatedBy
            FROM tblApiApps
            WHERE KeyHash = %s AND Void = 0
            """,
            (key_hash,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        perms = self._permissions_by_app([int(row["Id"])])
        return self._row_to_dict(row, perms.get(int(row["Id"]), []))

    def create(
        self,
        *,
        name: str,
        description: str | None,
        key_prefix: str,
        key_hash: str,
        permissions: list[str],
        expires_at: datetime | None,
        created_by: int,
    ) -> dict:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblApiApps (Name, Description, KeyPrefix, KeyHash, IsActive, ExpiresAt, CreatedBy)
            VALUES (%s, %s, %s, %s, 1, %s, %s)
            """,
            (name, description, key_prefix, key_hash, expires_at, created_by),
        )
        app_id = int(cursor.lastrowid)
        self._replace_permissions(cursor, app_id, permissions)
        self.db.commit()
        return self.get_by_id(app_id)  # type: ignore[return-value]

    def _replace_permissions(self, cursor, app_id: int, permissions: list[str]) -> None:
        cursor.execute("DELETE FROM tblApiAppPermissions WHERE AppId = %s", (app_id,))
        for code in ordered(permissions):
            cursor.execute(
                "INSERT INTO tblApiAppPermissions (AppId, PermissionCode) VALUES (%s, %s)",
                (app_id, code),
            )

    def update(self, app_id: int, changes: dict) -> dict | None:
        cursor = self.db.cursor()
        assignments: list[str] = []
        values: list = []
        column_map = {
            "name": "Name",
            "description": "Description",
            "is_active": "IsActive",
            "expires_at": "ExpiresAt",
        }
        for key, column in column_map.items():
            if key in changes:
                assignments.append(f"{column} = %s")
                values.append(changes[key])
        if assignments or "permissions" in changes:
            assignments.append("UpdatedAt = SYSUTCDATETIME()")
            values.append(app_id)
            cursor.execute(
                f"UPDATE tblApiApps SET {', '.join(assignments)} WHERE Id = %s AND Void = 0",
                tuple(values),
            )
            if cursor.rowcount == 0:
                return None
        if "permissions" in changes:
            self._replace_permissions(cursor, app_id, changes["permissions"])
        self.db.commit()
        return self.get_by_id(app_id)

    def rotate_key(self, app_id: int, key_prefix: str, key_hash: str) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblApiApps
            SET KeyPrefix = %s, KeyHash = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Void = 0
            """,
            (key_prefix, key_hash, app_id),
        )
        if cursor.rowcount == 0:
            return None
        self.db.commit()
        return self.get_by_id(app_id)

    def revoke(self, app_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblApiApps
            SET Void = 1, IsActive = 0, RevokedAt = SYSUTCDATETIME(), UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Void = 0
            """,
            (app_id,),
        )
        changed = cursor.rowcount > 0
        self.db.commit()
        return changed

    def touch_last_used(self, app_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE tblApiApps SET LastUsedAt = SYSUTCDATETIME() WHERE Id = %s",
            (app_id,),
        )
        self.db.commit()
