from datetime import datetime
import json

# Only this many seconds pass between LastUsedAt writes, so a widget polling every 5 seconds writes at most once a minute.
TOUCH_INTERVAL_SECONDS = 60


class WidgetRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, (bytes, bytearray)):
            return bool(value[0]) if value else False
        return bool(value)

    @staticmethod
    def _parse_flow(raw) -> dict | None:
        if not raw:
            return None
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _row_to_dict(row: dict) -> dict:
        return {
            "id": int(row["Id"]),
            "name": row["Name"],
            "widget_type": row["WidgetType"],
            "match_id": int(row["MatchId"]) if row.get("MatchId") is not None else None,
            "season_id": int(row["SeasonId"]) if row.get("SeasonId") is not None else None,
            "token_prefix": row["TokenPrefix"],
            "token_hash": row["TokenHash"],
            "is_active": WidgetRepository._as_bool(row["IsActive"]),
            "expires_at": row.get("ExpiresAt"),
            "last_used_at": row.get("LastUsedAt"),
            "revoked_at": row.get("RevokedAt"),
            "flow_config": WidgetRepository._parse_flow(row.get("FlowConfigJson")),
            "created_at": row.get("CreatedAt"),
            "updated_at": row.get("UpdatedAt"),
            "created_by": int(row["CreatedBy"]),
        }

    _COLUMNS = """
        Id, Name, WidgetType, MatchId, SeasonId, TokenPrefix, TokenHash, IsActive,
        ExpiresAt, LastUsedAt, RevokedAt, FlowConfigJson, CreatedAt, UpdatedAt, CreatedBy
    """

    def list_all(self) -> list[dict]:
        cursor = self.db.cursor()
        cursor.execute(f"SELECT {self._COLUMNS} FROM tblWidgetTokens ORDER BY Id DESC")
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def get_by_id(self, widget_id: int) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(f"SELECT {self._COLUMNS} FROM tblWidgetTokens WHERE Id = %s", (widget_id,))
        row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def get_by_token_hash(self, token_hash: str) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(f"SELECT {self._COLUMNS} FROM tblWidgetTokens WHERE TokenHash = %s", (token_hash,))
        row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def create(
        self,
        *,
        name: str,
        widget_type: str,
        match_id: int | None,
        season_id: int | None,
        token_prefix: str,
        token_hash: str,
        expires_at: datetime | None,
        created_by: int,
        flow_config: dict | None = None,
    ) -> dict:
        flow_json = json.dumps(flow_config, separators=(",", ":")) if flow_config is not None else None
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblWidgetTokens
                (Name, WidgetType, MatchId, SeasonId, TokenPrefix, TokenHash, IsActive, ExpiresAt, FlowConfigJson, CreatedBy)
            VALUES (%s, %s, %s, %s, %s, %s, 1, %s, %s, %s)
            """,
            (name, widget_type, match_id, season_id, token_prefix, token_hash, expires_at, flow_json, created_by),
        )
        widget_id = int(cursor.lastrowid)
        self.db.commit()
        return self.get_by_id(widget_id)  # type: ignore[return-value]

    def update(self, widget_id: int, changes: dict) -> dict | None:
        cursor = self.db.cursor()
        assignments: list[str] = []
        values: list = []
        if "name" in changes:
            assignments.append("Name = %s")
            values.append(changes["name"])
        if "expires_at" in changes:
            assignments.append("ExpiresAt = %s")
            values.append(changes["expires_at"])
        if "flow_config" in changes:
            flow_json = (
                json.dumps(changes["flow_config"], separators=(",", ":"))
                if changes["flow_config"] is not None
                else None
            )
            assignments.append("FlowConfigJson = %s")
            values.append(flow_json)
        if "is_active" in changes:
            assignments.append("IsActive = %s")
            values.append(1 if changes["is_active"] else 0)
            # Turning a link off stamps when it was revoked; turning it back on clears the stamp.
            assignments.append("RevokedAt = CASE WHEN %s = 1 THEN NULL ELSE COALESCE(RevokedAt, SYSUTCDATETIME()) END")
            values.append(1 if changes["is_active"] else 0)
        if not assignments:
            return self.get_by_id(widget_id)
        assignments.append("UpdatedAt = SYSUTCDATETIME()")
        values.append(widget_id)
        cursor.execute(f"UPDATE tblWidgetTokens SET {', '.join(assignments)} WHERE Id = %s", tuple(values))
        if cursor.rowcount == 0:
            return None
        self.db.commit()
        return self.get_by_id(widget_id)

    def rotate(self, widget_id: int, token_prefix: str, token_hash: str) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblWidgetTokens
            SET TokenPrefix = %s, TokenHash = %s, IsActive = 1, RevokedAt = NULL, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s
            """,
            (token_prefix, token_hash, widget_id),
        )
        if cursor.rowcount == 0:
            return None
        self.db.commit()
        return self.get_by_id(widget_id)

    def touch_last_used(self, widget_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblWidgetTokens
            SET LastUsedAt = SYSUTCDATETIME()
            WHERE Id = %s
              AND (LastUsedAt IS NULL OR LastUsedAt < DATEADD(SECOND, -%s, SYSUTCDATETIME()))
            """,
            (widget_id, TOUCH_INTERVAL_SECONDS),
        )
        self.db.commit()
