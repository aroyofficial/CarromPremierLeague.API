from core.permissions import ALL_CODES, CATALOG, ordered


class PermissionRepository:
    def __init__(self, db):
        self.db = db

    def sync_catalog(self) -> None:
        cursor = self.db.cursor()
        for permission in CATALOG:
            cursor.execute(
                """
                UPDATE tblPermissions
                SET Area = %s, Name = %s, Description = %s, SortOrder = %s, UpdatedAt = SYSUTCDATETIME()
                WHERE Code = %s
                  AND (Area <> %s OR Name <> %s OR Description <> %s OR SortOrder <> %s)
                """,
                (
                    permission.area, permission.name, permission.description, permission.sort_order,
                    permission.code,
                    permission.area, permission.name, permission.description, permission.sort_order,
                ),
            )
            cursor.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tblPermissions WHERE Code = %s)
                    INSERT INTO tblPermissions (Code, Area, Name, Description, SortOrder)
                    VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    permission.code,
                    permission.code, permission.area, permission.name, permission.description, permission.sort_order,
                ),
            )
        self.db.commit()

    def codes_for(self, admin_id: int) -> list[str]:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT PermissionCode FROM tblAdminPermissions WHERE AdminId = %s",
            (admin_id,),
        )
        return ordered(row["PermissionCode"] for row in cursor.fetchall() if row["PermissionCode"] in ALL_CODES)

    def codes_by_admin(self) -> dict[int, list[str]]:
        cursor = self.db.cursor()
        cursor.execute("SELECT AdminId, PermissionCode FROM tblAdminPermissions")
        grouped: dict[int, set[str]] = {}
        for row in cursor.fetchall():
            if row["PermissionCode"] in ALL_CODES:
                grouped.setdefault(int(row["AdminId"]), set()).add(row["PermissionCode"])
        return {admin_id: ordered(codes) for admin_id, codes in grouped.items()}

    def active_holder_ids(self, code: str, lock: bool = False) -> set[int]:
        cursor = self.db.cursor()
        lock_hint = " WITH (UPDLOCK, HOLDLOCK)" if lock else ""
        cursor.execute(
            f"""
            SELECT ap.AdminId
            FROM tblAdminPermissions ap{lock_hint}
            JOIN tblAdmins a ON a.Id = ap.AdminId
            WHERE ap.PermissionCode = %s AND a.Void = 0 AND a.IsActive = 1
            """,
            (code,),
        )
        return {int(row["AdminId"]) for row in cursor.fetchall()}

    def change(self, admin_id: int, grant: set[str], revoke: set[str], granted_by: int | None) -> None:
        cursor = self.db.cursor()
        for code in ordered(revoke):
            cursor.execute(
                "DELETE FROM tblAdminPermissions WHERE AdminId = %s AND PermissionCode = %s",
                (admin_id, code),
            )
        for code in ordered(grant):
            cursor.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tblAdminPermissions WHERE AdminId = %s AND PermissionCode = %s)
                    INSERT INTO tblAdminPermissions (AdminId, PermissionCode, GrantedBy)
                    VALUES (%s, %s, %s)
                """,
                (admin_id, code, admin_id, code, granted_by),
            )
        self.db.commit()
