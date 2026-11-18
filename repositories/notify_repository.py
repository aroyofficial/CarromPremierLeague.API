"""Persistence for push subscriptions, calendar feeds, and the outbox.

Match requests only insert outbox rows. Sending happens later, on another
connection, so a slow push service cannot hold the match update open.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime

import pyodbc

from services.notify.events import idempotency_key
from services.notify.retry import MAX_ATTEMPTS, retry_delay_seconds

logger = logging.getLogger(__name__)


def _jsonable(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def snapshot(match: dict | None) -> dict | None:
    if not match:
        return None
    return {key: _jsonable(value) for key, value in match.items()}


class NotifyRepository:

    def __init__(self, db):
        self.db = db

    def match_notice(self, match_id: int) -> dict | None:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                m.Id, m.SeasonId, s.Name AS SeasonName,
                m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                m.ScheduledDate, m.Status, m.Void, m.Category, m.Outcome, m.NetPoints,
                m.UpdatedAt, m.StartedAt, m.CreatedAt
            FROM tblMatches m
            JOIN tblSeasons s ON s.Id = m.SeasonId
            LEFT JOIN tblTeams t1 ON t1.Id = m.Team1
            LEFT JOIN tblTeams t2 ON t2.Id = m.Team2
            WHERE m.Id = %s
            """,
            (match_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": int(row["Id"]),
            "season_id": int(row["SeasonId"]),
            "season_name": row["SeasonName"] or "",
            "team1_id": int(row["Team1"]) if row["Team1"] is not None else None,
            "team2_id": int(row["Team2"]) if row["Team2"] is not None else None,
            "team1_name": row["Team1Name"] or "TBD",
            "team2_name": row["Team2Name"] or "TBD",
            "scheduled_date": row["ScheduledDate"],
            "status": int(row["Status"]),
            "void": bool(row["Void"]),
            "category": int(row["Category"]),
            "outcome": int(row["Outcome"]) if row["Outcome"] is not None else 3,
            "net_points": int(row["NetPoints"]) if row["NetPoints"] is not None else None,
            "updated_at": row["UpdatedAt"],
            "started_at": row["StartedAt"],
            "created_at": row["CreatedAt"],
        }

    def enqueue(
        self,
        match: dict,
        event: str,
        before: dict | None = None,
        key: str | None = None,
        connection_id: int | None = None,
    ) -> int | None:
        key = key or idempotency_key(int(match["id"]), event, match.get("updated_at"))
        notice = {"event": event, "match": snapshot(match), "before": snapshot(before)}
        if connection_id is not None:
            notice["connection_id"] = int(connection_id)
        payload = json.dumps(notice, separators=(",", ":"))
        cursor = self.db.cursor()
        # Never roll back here: the connection may carry the caller's match write. A duplicate
        # key is skipped by the NOT EXISTS guard; a concurrent duplicate fails only this
        # statement (SQL Server leaves the rest of the transaction intact).
        try:
            cursor.execute(
                """
                INSERT INTO tblNotificationOutbox (MatchId, EventType, IdempotencyKey, Payload)
                SELECT %s, %s, %s, %s
                WHERE NOT EXISTS (SELECT 1 FROM tblNotificationOutbox WHERE IdempotencyKey = %s)
                """,
                (int(match["id"]), event, key, payload, key),
            )
        except pyodbc.IntegrityError:
            logger.info("duplicate match notice match=%s event=%s", match.get("id"), event)
            return None
        self.db.commit()
        if cursor.lastrowid is None:
            logger.info("duplicate match notice match=%s event=%s", match.get("id"), event)
            return None
        return cursor.lastrowid

    def claim_pending(self, limit: int = 20) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        # A worker that died while holding a lease leaves the row 'leased'. Once the lease
        # has expired the row is reclaimed, so a crash cannot strand a notice. A row whose
        # last allowed attempt was the crashed one is dead.
        # AvailableAt is DATETIME2(6) and rounds on insert, so "now" is compared at the
        # same precision. Otherwise a fresh row can sit just ahead of the clock.
        cursor.execute(
            """
            UPDATE tblNotificationOutbox
            SET Status = 'dead', LastError = 'lease expired after the last attempt'
            WHERE Status = 'leased' AND AvailableAt <= CAST(SYSUTCDATETIME() AS DATETIME2(6)) AND Attempts >= %s
            """,
            (MAX_ATTEMPTS,),
        )
        cursor.execute(
            """
            SELECT TOP (20) Id
            FROM tblNotificationOutbox
            WHERE Status IN ('pending', 'leased') AND AvailableAt <= CAST(SYSUTCDATETIME() AS DATETIME2(6)) AND Attempts < %s
            ORDER BY Id
            """,
            (MAX_ATTEMPTS,),
        )
        ids = [int(row["Id"]) for row in cursor.fetchall()]
        claimed: list[int] = []
        for item_id in ids[:limit]:
            # The AvailableAt guard means a row another worker just leased is skipped.
            cursor.execute(
                """
                UPDATE tblNotificationOutbox
                SET Status = 'leased', Attempts = Attempts + 1,
                    AvailableAt = DATEADD(MINUTE, 5, SYSUTCDATETIME())
                WHERE Id = %s AND Status IN ('pending', 'leased')
                  AND AvailableAt <= CAST(SYSUTCDATETIME() AS DATETIME2(6)) AND Attempts < %s
                """,
                (item_id, MAX_ATTEMPTS),
            )
            if cursor.rowcount:
                claimed.append(item_id)
        self.db.commit()
        if not claimed:
            return []
        placeholders = ", ".join(["%s"] * len(claimed))
        cursor.execute(
            f"""
            SELECT Id, MatchId, EventType, Payload, Attempts
            FROM tblNotificationOutbox
            WHERE Id IN ({placeholders})
            ORDER BY Id
            """,
            tuple(claimed),
        )
        return cursor.fetchall()

    def mark_done(self, item_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblNotificationOutbox
            SET Status = 'done', LastError = NULL
            WHERE Id = %s
            """,
            (item_id,),
        )
        self.db.commit()

    def mark_retry(self, item_id: int, attempts: int, reason: str, retry_after: int | None = None) -> None:
        status = "dead" if attempts >= MAX_ATTEMPTS else "pending"
        delay = retry_delay_seconds(attempts, retry_after)
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblNotificationOutbox
            SET Status = %s,
                AvailableAt = DATEADD(SECOND, %s, SYSUTCDATETIME()),
                LastError = %s
            WHERE Id = %s
            """,
            (status, delay, reason[:400], item_id),
        )
        self.db.commit()

    def set_connection_status(self, connection_id: int, status: str) -> None:
        """Stop using a calendar connection, for example after its grant was revoked."""
        if status not in ("revoked", "replaced"):
            raise ValueError("A connection can only move to revoked or replaced here.")
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblCalendarConnections
            SET Status = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Status = 'active'
            """,
            (status, connection_id),
        )
        self.db.commit()

    def record_delivery(self, outbox_id: int, channel: str, target: str, status: str, detail: str) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblNotificationDeliveries (OutboxId, Channel, Target, Status, Detail)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (outbox_id, channel, (target or "")[:200], status, (detail or "")[:400]),
        )
        self.db.commit()

    def sent_targets(self, outbox_id: int, channel: str) -> set[str]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            "SELECT DISTINCT Target FROM tblNotificationDeliveries WHERE OutboxId = %s AND Channel = %s AND Status = 'sent'",
            (outbox_id, channel),
        )
        return {str(row["Target"]) for row in cursor.fetchall()}

    def delivery_health(self) -> dict:
        """Counts an operator needs: backlog, dead notices, live subscriptions, and connection states."""
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM tblNotificationOutbox WHERE Status IN ('pending', 'leased')) AS Backlog,
                (SELECT COUNT(*) FROM tblNotificationOutbox WHERE Status = 'dead') AS Dead,
                (SELECT DATEDIFF(SECOND, MIN(CreatedAt), SYSUTCDATETIME())
                   FROM tblNotificationOutbox WHERE Status IN ('pending', 'leased')) AS OldestPendingSeconds,
                (SELECT COUNT(*) FROM tblPushSubscriptions WHERE Void = 0) AS ActiveSubscriptions,
                (SELECT COUNT(*) FROM tblCalendarConnections WHERE Void = 0 AND Status = 'active') AS ActiveConnections,
                (SELECT COUNT(*) FROM tblCalendarConnections WHERE Void = 0 AND Status = 'revoked') AS ConnectionsNeedingReauth,
                (SELECT COUNT(*) FROM tblNotificationDeliveries
                   WHERE Status = 'failed' AND CreatedAt >= DATEADD(HOUR, -24, SYSUTCDATETIME())) AS FailedLast24h,
                (SELECT COUNT(*) FROM tblNotificationDeliveries
                   WHERE Status = 'sent' AND CreatedAt >= DATEADD(HOUR, -24, SYSUTCDATETIME())) AS SentLast24h
            """
        )
        row = cursor.fetchone() or {}
        return {
            "backlog": int(row.get("Backlog") or 0),
            "dead": int(row.get("Dead") or 0),
            "oldest_pending_seconds": int(row["OldestPendingSeconds"]) if row.get("OldestPendingSeconds") is not None else None,
            "active_subscriptions": int(row.get("ActiveSubscriptions") or 0),
            "active_connections": int(row.get("ActiveConnections") or 0),
            "connections_needing_reauth": int(row.get("ConnectionsNeedingReauth") or 0),
            "failed_last_24h": int(row.get("FailedLast24h") or 0),
            "sent_last_24h": int(row.get("SentLast24h") or 0),
        }

    def connection_sync_states(self, connection_ids: list[int]) -> dict[int, dict]:
        """Last delivery per connection, plus whether a notice is still queued. Drives the sync state shown to the admin."""
        if not connection_ids:
            return {}
        placeholders = ", ".join(["%s"] * len(connection_ids))
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            f"""
            SELECT d.Target, d.Status, d.Detail, d.CreatedAt
            FROM tblNotificationDeliveries d
            JOIN (
                SELECT Target, MAX(Id) AS LastId
                FROM tblNotificationDeliveries
                WHERE Channel IN ('google', 'microsoft') AND Target IN ({placeholders})
                GROUP BY Target
            ) last ON last.LastId = d.Id
            """,
            tuple(str(int(cid)) for cid in connection_ids),
        )
        states = {
            int(row["Target"]): {"last_status": row["Status"], "last_detail": row["Detail"] or "", "last_at": row["CreatedAt"]}
            for row in cursor.fetchall()
        }
        cursor.execute("SELECT COUNT(*) AS Pending FROM tblNotificationOutbox WHERE Status IN ('pending', 'leased')")
        pending = int((cursor.fetchone() or {}).get("Pending") or 0)
        for cid in connection_ids:
            states.setdefault(int(cid), {"last_status": None, "last_detail": "", "last_at": None})
            states[int(cid)]["queue_pending"] = pending > 0
        return states

    def save_subscription(self, row: dict) -> dict:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            "SELECT Id FROM tblPushSubscriptions WHERE EndpointHash = %s",
            (endpoint_hash(row["endpoint"]),),
        )
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                """
                UPDATE tblPushSubscriptions
                SET P256dh = %s, AuthSecret = %s, AdminId = %s, SeasonId = %s, TeamId = %s,
                    UserAgent = %s, Void = 0, FailureCount = 0,
                    NotifyScheduled = %s, NotifyChanged = %s, NotifyCancelled = %s,
                    NotifyLive = %s, NotifyEnded = %s, NotifyScore = %s, NotifyReminder = %s,
                    UpdatedAt = SYSUTCDATETIME()
                WHERE Id = %s
                """,
                (
                    row["p256dh"], row["auth"], row.get("admin_id"), row.get("season_id"), row.get("team_id"),
                    row.get("user_agent"),
                    int(row["notify_scheduled"]), int(row["notify_changed"]), int(row["notify_cancelled"]),
                    int(row["notify_live"]), int(row["notify_ended"]), int(row["notify_score"]),
                    int(row["notify_reminder"]),
                    int(existing["Id"]),
                ),
            )
            self.db.commit()
            return self.subscription_by_endpoint(row["endpoint"])
        cursor.execute(
            """
            INSERT INTO tblPushSubscriptions (
                Endpoint, EndpointHash, P256dh, AuthSecret, AdminId, SeasonId, TeamId, UserAgent,
                NotifyScheduled, NotifyChanged, NotifyCancelled, NotifyLive, NotifyEnded, NotifyScore, NotifyReminder
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row["endpoint"], endpoint_hash(row["endpoint"]), row["p256dh"], row["auth"], row.get("admin_id"), row.get("season_id"),
                row.get("team_id"), row.get("user_agent"),
                int(row["notify_scheduled"]), int(row["notify_changed"]), int(row["notify_cancelled"]),
                int(row["notify_live"]), int(row["notify_ended"]), int(row["notify_score"]),
                int(row["notify_reminder"]),
            ),
        )
        self.db.commit()
        return self.subscription_by_endpoint(row["endpoint"])

    def subscription_by_endpoint(self, endpoint: str) -> dict | None:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, Endpoint, P256dh, AuthSecret, AdminId, SeasonId, TeamId, Void, FailureCount,
                   NotifyScheduled, NotifyChanged, NotifyCancelled, NotifyLive, NotifyEnded, NotifyScore, NotifyReminder
            FROM tblPushSubscriptions
            WHERE EndpointHash = %s
            """,
            (endpoint_hash(endpoint),),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return _subscription(row)

    def update_preferences(self, endpoint: str, preferences: dict) -> dict | None:
        current = self.subscription_by_endpoint(endpoint)
        if not current or current["void"]:
            return None
        merged = dict(current)
        merged.update(preferences)
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblPushSubscriptions
            SET NotifyScheduled = %s, NotifyChanged = %s, NotifyCancelled = %s,
                NotifyLive = %s, NotifyEnded = %s, NotifyScore = %s, NotifyReminder = %s,
                SeasonId = %s, TeamId = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE EndpointHash = %s AND Void = 0
            """,
            (
                int(merged["notify_scheduled"]), int(merged["notify_changed"]), int(merged["notify_cancelled"]),
                int(merged["notify_live"]), int(merged["notify_ended"]), int(merged["notify_score"]),
                int(merged["notify_reminder"]),
                merged.get("season_id"), merged.get("team_id"), endpoint_hash(endpoint),
            ),
        )
        self.db.commit()
        return self.subscription_by_endpoint(endpoint)

    def void_subscription(self, endpoint: str) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblPushSubscriptions
            SET Void = 1, UpdatedAt = SYSUTCDATETIME()
            WHERE EndpointHash = %s AND Void = 0
            """,
            (endpoint_hash(endpoint),),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def note_push_failure(self, subscription_id: int, gone: bool) -> None:
        cursor = self.db.cursor()
        if gone:
            cursor.execute(
                "UPDATE tblPushSubscriptions SET Void = 1, FailureCount = FailureCount + 1, UpdatedAt = SYSUTCDATETIME() WHERE Id = %s",
                (subscription_id,),
            )
        else:
            cursor.execute(
                """
                UPDATE tblPushSubscriptions
                SET FailureCount = FailureCount + 1,
                    Void = CASE WHEN FailureCount + 1 >= 8 THEN 1 ELSE Void END,
                    UpdatedAt = SYSUTCDATETIME()
                WHERE Id = %s
                """,
                (subscription_id,),
            )
        self.db.commit()

    def active_subscriptions(self) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, Endpoint, P256dh, AuthSecret, AdminId, SeasonId, TeamId, Void, FailureCount,
                   NotifyScheduled, NotifyChanged, NotifyCancelled, NotifyLive, NotifyEnded, NotifyScore, NotifyReminder
            FROM tblPushSubscriptions
            WHERE Void = 0
            """
        )
        return [_subscription(row) for row in cursor.fetchall()]

    def season_exists(self, season_id: int) -> bool:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("SELECT Id, Name FROM tblSeasons WHERE Id = %s AND Void = 0", (season_id,))
        return cursor.fetchone() is not None

    def team_exists(self, team_id: int) -> bool:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("SELECT Id FROM tblTeams WHERE Id = %s AND Void = 0", (team_id,))
        return cursor.fetchone() is not None

    def create_feed(self, season_id: int, team_id: int | None, token: str, label: str) -> dict:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO tblCalendarFeeds (Token, SeasonId, TeamId, Label)
            VALUES (%s, %s, %s, %s)
            """,
            (token_hash(token), season_id, team_id, label[:120]),
        )
        self.db.commit()
        return {"token": token, "season_id": season_id, "team_id": team_id, "label": label}

    def feed_by_token(self, token: str) -> dict | None:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, SeasonId, TeamId, Label, Void
            FROM tblCalendarFeeds
            WHERE Token = %s
            """,
            (token_hash(token),),
        )
        row = cursor.fetchone()
        if not row or bool(row["Void"]):
            return None
        return {
            "id": int(row["Id"]),
            "season_id": int(row["SeasonId"]),
            "team_id": int(row["TeamId"]) if row["TeamId"] is not None else None,
            "label": row["Label"] or "Carrom Premier League",
        }

    def revoke_feed(self, token: str) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE tblCalendarFeeds SET Void = 1, UpdatedAt = SYSUTCDATETIME() WHERE Token = %s AND Void = 0",
            (token_hash(token),),
        )
        self.db.commit()
        return cursor.rowcount > 0

    def feed_matches(self, season_id: int, team_id: int | None) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        if team_id is None:
            cursor.execute(
                """
                SELECT
                    m.Id, m.SeasonId, s.Name AS SeasonName,
                    m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                    m.ScheduledDate, m.Status, m.Void, m.Category, m.Outcome, m.NetPoints,
                    m.UpdatedAt, m.StartedAt, m.CreatedAt
                FROM tblMatches m
                JOIN tblSeasons s ON s.Id = m.SeasonId
                LEFT JOIN tblTeams t1 ON t1.Id = m.Team1
                LEFT JOIN tblTeams t2 ON t2.Id = m.Team2
                WHERE m.SeasonId = %s
                ORDER BY m.ScheduledDate, m.[Order], m.Id
                """,
                (season_id,),
            )
        else:
            cursor.execute(
                """
                SELECT
                    m.Id, m.SeasonId, s.Name AS SeasonName,
                    m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                    m.ScheduledDate, m.Status, m.Void, m.Category, m.Outcome, m.NetPoints,
                    m.UpdatedAt, m.StartedAt, m.CreatedAt
                FROM tblMatches m
                JOIN tblSeasons s ON s.Id = m.SeasonId
                LEFT JOIN tblTeams t1 ON t1.Id = m.Team1
                LEFT JOIN tblTeams t2 ON t2.Id = m.Team2
                WHERE m.SeasonId = %s AND (m.Team1 = %s OR m.Team2 = %s)
                ORDER BY m.ScheduledDate, m.[Order], m.Id
                """,
                (season_id, team_id, team_id),
            )
        return [self._row_notice(row) for row in cursor.fetchall()]

    def due_reminders(self, day: date) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                m.Id, m.SeasonId, s.Name AS SeasonName,
                m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                m.ScheduledDate, m.Status, m.Void, m.Category, m.Outcome, m.NetPoints,
                m.UpdatedAt, m.StartedAt, m.CreatedAt
            FROM tblMatches m
            JOIN tblSeasons s ON s.Id = m.SeasonId
            LEFT JOIN tblTeams t1 ON t1.Id = m.Team1
            LEFT JOIN tblTeams t2 ON t2.Id = m.Team2
            WHERE m.Void = 0 AND m.Status = 1 AND m.ScheduledDate = %s
            """,
            (day,),
        )
        return [self._row_notice(row) for row in cursor.fetchall()]

    def active_connections(self) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, AdminId, Provider, ExternalCalendarId, RefreshToken, Status, Void
            FROM tblCalendarConnections
            WHERE Void = 0 AND Status = 'active'
            """
        )
        return [
            {
                "id": int(row["Id"]),
                "admin_id": int(row["AdminId"]),
                "provider": row["Provider"],
                "calendar_id": row["ExternalCalendarId"],
                "refresh_token": row["RefreshToken"],
                "void": bool(row["Void"]),
            }
            for row in cursor.fetchall()
        ]

    def connections_for_admin(self, admin_id: int) -> list[dict]:
        """The admin's own connections, without any token. Used for the connection status view."""
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, Provider, Status, UpdatedAt
            FROM tblCalendarConnections
            WHERE AdminId = %s AND Void = 0
            ORDER BY Provider
            """,
            (admin_id,),
        )
        return [
            {
                "id": int(row["Id"]),
                "provider": row["Provider"],
                "status": row["Status"],
                "updated_at": row["UpdatedAt"],
            }
            for row in cursor.fetchall()
        ]

    def connection_for_admin(self, connection_id: int, admin_id: int) -> dict | None:
        """One of the admin's own connections, with its encrypted token. Never another admin's."""
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, Provider, Status, RefreshToken
            FROM tblCalendarConnections
            WHERE Id = %s AND AdminId = %s AND Void = 0
            """,
            (connection_id, admin_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {
            "id": int(row["Id"]),
            "provider": row["Provider"],
            "status": row["Status"],
            "refresh_token": row["RefreshToken"],
        }

    def disconnect_connection(self, connection_id: int, admin_id: int) -> bool:
        """Void the connection and blank the stored token. Calendar events already written stay put."""
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblCalendarConnections
            SET Void = 1, Status = 'revoked', RefreshToken = '', UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND AdminId = %s AND Void = 0
            """,
            (connection_id, admin_id),
        )
        changed = cursor.rowcount > 0
        self.db.commit()
        return changed

    def save_connection(self, admin_id: int, provider: str, refresh_token: str, calendar_id: str) -> int | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblCalendarConnections
            SET Void = 1, Status = 'replaced', UpdatedAt = SYSUTCDATETIME()
            WHERE AdminId = %s AND Provider = %s AND Void = 0
            """,
            (admin_id, provider),
        )
        cursor.execute(
            """
            INSERT INTO tblCalendarConnections (AdminId, Provider, ExternalCalendarId, RefreshToken, Status)
            VALUES (%s, %s, %s, %s, 'active')
            """,
            (admin_id, provider, calendar_id, refresh_token),
        )
        connection_id = cursor.lastrowid
        self.db.commit()
        return connection_id

    def upcoming_matches(self, day: date, limit: int = 200) -> list[dict]:
        """Matches still to be played or in play, from `day` on. Used to fill a newly connected calendar."""
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            f"""
            SELECT TOP ({int(limit)})
                m.Id, m.SeasonId, s.Name AS SeasonName,
                m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                m.ScheduledDate, m.Status, m.Void, m.Category, m.Outcome, m.NetPoints,
                m.UpdatedAt, m.StartedAt, m.CreatedAt
            FROM tblMatches m
            JOIN tblSeasons s ON s.Id = m.SeasonId
            LEFT JOIN tblTeams t1 ON t1.Id = m.Team1
            LEFT JOIN tblTeams t2 ON t2.Id = m.Team2
            WHERE m.Void = 0 AND m.Status IN (1, 2) AND m.ScheduledDate >= %s
            ORDER BY m.ScheduledDate, m.[Order], m.Id
            """,
            (day,),
        )
        return [self._row_notice(row) for row in cursor.fetchall()]

    def season_scheduled(self, season_id: int) -> list[dict]:
        """The season's unplayed matches: the rows a schedule rebuild deletes and inserts."""
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT
                m.Id, m.SeasonId, s.Name AS SeasonName,
                m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                m.ScheduledDate, m.Status, m.Void, m.Category, m.Outcome, m.NetPoints,
                m.UpdatedAt, m.StartedAt, m.CreatedAt
            FROM tblMatches m
            JOIN tblSeasons s ON s.Id = m.SeasonId
            LEFT JOIN tblTeams t1 ON t1.Id = m.Team1
            LEFT JOIN tblTeams t2 ON t2.Id = m.Team2
            WHERE m.SeasonId = %s AND m.Void = 0 AND m.Status = 1
            ORDER BY m.ScheduledDate, m.[Order], m.Id
            """,
            (season_id,),
        )
        return [self._row_notice(row) for row in cursor.fetchall()]

    def event_map(self, connection_id: int, match_id: int) -> dict | None:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT ExternalEventId, ContentHash
            FROM tblCalendarEventMaps
            WHERE ConnectionId = %s AND MatchId = %s
            """,
            (connection_id, match_id),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return {"external_event_id": row["ExternalEventId"], "content_hash": row["ContentHash"]}

    def save_event_map(self, connection_id: int, match_id: int, external_event_id: str, content_hash: str) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblCalendarEventMaps
            SET ExternalEventId = %s, ContentHash = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE ConnectionId = %s AND MatchId = %s
            """,
            (external_event_id, content_hash, connection_id, match_id),
        )
        if cursor.rowcount == 0:
            cursor.execute(
                """
                INSERT INTO tblCalendarEventMaps (ConnectionId, MatchId, ExternalEventId, ContentHash)
                VALUES (%s, %s, %s, %s)
                """,
                (connection_id, match_id, external_event_id, content_hash),
            )
        self.db.commit()

    @staticmethod
    def _row_notice(row: dict) -> dict:
        return {
            "id": int(row["Id"]),
            "season_id": int(row["SeasonId"]),
            "season_name": row["SeasonName"] or "",
            "team1_id": int(row["Team1"]) if row["Team1"] is not None else None,
            "team2_id": int(row["Team2"]) if row["Team2"] is not None else None,
            "team1_name": row["Team1Name"] or "TBD",
            "team2_name": row["Team2Name"] or "TBD",
            "scheduled_date": row["ScheduledDate"],
            "status": int(row["Status"]),
            "void": bool(row["Void"]),
            "category": int(row["Category"]),
            "outcome": int(row["Outcome"]) if row["Outcome"] is not None else 3,
            "net_points": int(row["NetPoints"]) if row["NetPoints"] is not None else None,
            "updated_at": row["UpdatedAt"],
            "started_at": row["StartedAt"],
            "created_at": row["CreatedAt"],
        }


def endpoint_hash(endpoint: str) -> str:
    """The unique key for a push endpoint. Endpoints can exceed SQL Server's 1700-byte index key."""
    return hashlib.sha256(str(endpoint).encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    """Feed tokens are stored hashed. A copy of the table cannot be used to read a feed."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _subscription(row: dict) -> dict:
    return {
        "id": int(row["Id"]),
        "endpoint": row["Endpoint"],
        "p256dh": row["P256dh"],
        "auth": row["AuthSecret"],
        "admin_id": int(row["AdminId"]) if row["AdminId"] is not None else None,
        "season_id": int(row["SeasonId"]) if row["SeasonId"] is not None else None,
        "team_id": int(row["TeamId"]) if row["TeamId"] is not None else None,
        "void": bool(row["Void"]),
        "failure_count": int(row["FailureCount"] or 0),
        "notify_scheduled": bool(row["NotifyScheduled"]),
        "notify_changed": bool(row["NotifyChanged"]),
        "notify_cancelled": bool(row["NotifyCancelled"]),
        "notify_live": bool(row["NotifyLive"]),
        "notify_ended": bool(row["NotifyEnded"]),
        "notify_score": bool(row["NotifyScore"]),
        "notify_reminder": bool(row["NotifyReminder"]),
    }


def public_subscription(row: dict) -> dict:
    hidden = dict(row)
    hidden.pop("endpoint", None)
    hidden.pop("p256dh", None)
    hidden.pop("auth", None)
    return hidden
