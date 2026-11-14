from core.permissions import ALL_CODES, AREA_NAMES, AREAS, CATALOG, ROLES, SECTIONS, ordered


class PermissionRepository:
    def __init__(self, db):
        self.db = db

    def sync_catalog(self) -> None:
        cursor = self.db.cursor()
        for section in SECTIONS:
            cursor.execute(
                """
                UPDATE tblPermissionSections
                SET Name = %s, Description = %s, SortOrder = %s, UpdatedAt = SYSUTCDATETIME()
                WHERE Code = %s AND (Name <> %s OR Description <> %s OR SortOrder <> %s)
                """,
                (
                    section.name, section.description, section.sort_order,
                    section.code,
                    section.name, section.description, section.sort_order,
                ),
            )
            cursor.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tblPermissionSections WHERE Code = %s)
                    INSERT INTO tblPermissionSections (Code, Name, Description, SortOrder)
                    VALUES (%s, %s, %s, %s)
                """,
                (section.code, section.code, section.name, section.description, section.sort_order),
            )
        for area in AREAS:
            cursor.execute(
                """
                UPDATE tblPermissionAreas
                SET SectionCode = %s, Name = %s, Description = %s, Icon = %s, SortOrder = %s,
                    UpdatedAt = SYSUTCDATETIME()
                WHERE Code = %s
                  AND (SectionCode <> %s OR Name <> %s OR Description <> %s OR Icon <> %s OR SortOrder <> %s)
                """,
                (
                    area.section, area.name, area.description, area.icon, area.sort_order,
                    area.code,
                    area.section, area.name, area.description, area.icon, area.sort_order,
                ),
            )
            cursor.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tblPermissionAreas WHERE Code = %s)
                    INSERT INTO tblPermissionAreas (Code, SectionCode, Name, Description, Icon, SortOrder)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (area.code, area.code, area.section, area.name, area.description, area.icon, area.sort_order),
            )
        for permission in CATALOG:
            area_name = AREA_NAMES[permission.area]
            cursor.execute(
                """
                UPDATE tblPermissions
                SET Area = %s, AreaCode = %s, Name = %s, Description = %s, SortOrder = %s,
                    UpdatedAt = SYSUTCDATETIME()
                WHERE Code = %s
                  AND (Area <> %s OR ISNULL(AreaCode, '') <> %s OR Name <> %s OR Description <> %s
                       OR SortOrder <> %s)
                """,
                (
                    area_name, permission.area, permission.name, permission.description, permission.sort_order,
                    permission.code,
                    area_name, permission.area, permission.name, permission.description, permission.sort_order,
                ),
            )
            cursor.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tblPermissions WHERE Code = %s)
                    INSERT INTO tblPermissions (Code, Area, AreaCode, Name, Description, SortOrder)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    permission.code,
                    permission.code, area_name, permission.area, permission.name, permission.description,
                    permission.sort_order,
                ),
            )
        # Parents are set after every row exists, because ParentCode references tblPermissions.
        for permission in CATALOG:
            if permission.parent:
                cursor.execute(
                    """
                    UPDATE tblPermissions
                    SET ParentCode = %s, UpdatedAt = SYSUTCDATETIME()
                    WHERE Code = %s AND ISNULL(ParentCode, '') <> %s
                    """,
                    (permission.parent, permission.code, permission.parent),
                )
            else:
                cursor.execute(
                    """
                    UPDATE tblPermissions
                    SET ParentCode = NULL, UpdatedAt = SYSUTCDATETIME()
                    WHERE Code = %s AND ParentCode IS NOT NULL
                    """,
                    (permission.code,),
                )
        for role in ROLES:
            cursor.execute(
                """
                UPDATE tblRoles
                SET Name = %s, Description = %s, SortOrder = %s, UpdatedAt = SYSUTCDATETIME()
                WHERE Code = %s AND (Name <> %s OR Description <> %s OR SortOrder <> %s)
                """,
                (
                    role.name, role.description, role.sort_order,
                    role.code,
                    role.name, role.description, role.sort_order,
                ),
            )
            cursor.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tblRoles WHERE Code = %s)
                    INSERT INTO tblRoles (Code, Name, Description, SortOrder)
                    VALUES (%s, %s, %s, %s)
                """,
                (role.code, role.code, role.name, role.description, role.sort_order),
            )
            cursor.execute("SELECT PermissionCode FROM tblRolePermissions WHERE RoleCode = %s", (role.code,))
            stored = {row["PermissionCode"] for row in cursor.fetchall()}
            wanted = set(role.permissions)
            for code in stored - wanted:
                cursor.execute(
                    "DELETE FROM tblRolePermissions WHERE RoleCode = %s AND PermissionCode = %s",
                    (role.code, code),
                )
            for code in ordered(wanted - stored):
                cursor.execute(
                    "INSERT INTO tblRolePermissions (RoleCode, PermissionCode) VALUES (%s, %s)",
                    (role.code, code),
                )
        self._backfill_parents(cursor)
        self.db.commit()

    @staticmethod
    def _backfill_parents(cursor) -> None:
        cursor.execute(
            """
            INSERT INTO tblAdminPermissions (AdminId, PermissionCode)
            SELECT DISTINCT ap.AdminId, p.ParentCode
            FROM tblAdminPermissions ap
            JOIN tblPermissions p ON p.Code = ap.PermissionCode
            WHERE p.ParentCode IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM tblAdminPermissions held
                  WHERE held.AdminId = ap.AdminId AND held.PermissionCode = p.ParentCode
              )
            """
        )

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
