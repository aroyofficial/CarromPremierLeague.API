from core.security import PASSWORD_ITERATIONS, hash_password, verify_password


SEED_USERNAME = "admin"
SEED_PASSWORD = "Welcome@1234"


def _as_bool(value) -> bool:
    if isinstance(value, (bytes, bytearray)):
        return bool(value[0]) if value else False
    return bool(value)


def ensure_seed_admin(db, *, reset_password: bool = False) -> str:
    """Ensure the default admin exists and can sign in.

    A missing or deactivated account is created or restored with the seed
    password. An active account keeps a changed password unless reset_password
    is set.
    """
    cursor = db.cursor()
    cursor.execute(
        """
        SELECT Id, PasswordHash, PasswordSalt, PasswordIterations, IsActive, Void
        FROM tblAdmins
        WHERE Username = %s
        """,
        (SEED_USERNAME,),
    )
    row = cursor.fetchone()
    password_matches = bool(
        row
        and verify_password(
            SEED_PASSWORD,
            row["PasswordHash"],
            row["PasswordSalt"],
            int(row["PasswordIterations"]),
        )
    )
    active = bool(row) and _as_bool(row["IsActive"]) and not _as_bool(row["Void"])

    if row and active and password_matches:
        return "present"
    if row and active and not reset_password:
        return "present"

    password_hash, password_salt = hash_password(SEED_PASSWORD)
    if row is None:
        cursor.execute(
            """
            INSERT INTO tblAdmins (Username, PasswordHash, PasswordSalt, PasswordIterations)
            VALUES (%s, %s, %s, %s)
            """,
            (SEED_USERNAME, password_hash, password_salt, PASSWORD_ITERATIONS),
        )
        db.commit()
        return "created"

    cursor.execute(
        """
        UPDATE tblAdmins
        SET PasswordHash = %s,
            PasswordSalt = %s,
            PasswordIterations = %s,
            IsActive = 1,
            Void = 0,
            TokenVersion = TokenVersion + 1,
            UpdatedAt = SYSUTCDATETIME()
        WHERE Id = %s
        """,
        (password_hash, password_salt, PASSWORD_ITERATIONS, row["Id"]),
    )
    db.commit()
    return "restored"
