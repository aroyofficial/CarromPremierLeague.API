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


ROSTER_KEYS = ("team1_players", "team2_players")


def snapshot(match: dict | None) -> dict | None:
    """The match as stored in an outbox row. Roster names are left out, so queued rows hold no player names."""
    if not match:
        return None
    return {key: _jsonable(value) for key, value in match.items() if key not in ROSTER_KEYS}


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
        notice = {
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
        return self.attach_rosters([notice])[0]

    def roster_names(self, season_id: int, team_ids: list[int]) -> dict[int, list[str]]:
        """Active players on each team's roster for one season, by team id, in name order."""
        ids = sorted({int(team_id) for team_id in team_ids if team_id is not None})
        if not ids:
            return {}
        placeholders = ", ".join(["%s"] * len(ids))
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            f"""
            SELECT pst.TeamId, p.FirstName, p.LastName
            FROM tblPlayersSeasonsTeams pst
            JOIN tblPlayers p ON p.Id = pst.PlayerId AND p.Void = 0
            WHERE pst.SeasonId = %s AND pst.Void = 0 AND pst.TeamId IN ({placeholders})
            ORDER BY p.FirstName, p.LastName, p.Id
            """,
            (int(season_id), *ids),
        )
        names: dict[int, list[str]] = {}
        for row in cursor.fetchall():
            full = " ".join(part for part in ((row["FirstName"] or "").strip(), (row["LastName"] or "").strip()) if part)
            if full:
                names.setdefault(int(row["TeamId"]), []).append(full)
        return names

    def attach_rosters(self, notices: list[dict]) -> list[dict]:
        """Add team1_players and team2_players to each notice. One roster query per season.

        The names are added after the outbox snapshot is taken, so they are never stored
        in a queued row; the worker reads them again when it writes the calendar event.
        """
        by_season: dict[int, set[int]] = {}
        for notice in notices:
            team_ids = by_season.setdefault(int(notice["season_id"]), set())
            for key in ("team1_id", "team2_id"):
                if notice.get(key) is not None:
                    team_ids.add(int(notice[key]))
        rosters: dict[tuple[int, int], list[str]] = {}
        for season_id, team_ids in by_season.items():
            for team_id, names in self.roster_names(season_id, sorted(team_ids)).items():
                rosters[(season_id, team_id)] = names
        for notice in notices:
            season_id = int(notice["season_id"])
            team1 = notice.get("team1_id")
            team2 = notice.get("team2_id")
            notice["team1_players"] = rosters.get((season_id, int(team1)), []) if team1 is not None else []
            notice["team2_players"] = rosters.get((season_id, int(team2)), []) if team2 is not None else []
        return notices

    def enqueue(
        self,
        match: dict,
        event: str,
        before: dict | None = None,
        key: str | None = None,
        connection_id: int | None = None,
        existing_only: bool = False,
    ) -> int | None:
        key = key or idempotency_key(int(match["id"]), event, match.get("updated_at"))
        notice = {"event": event, "match": snapshot(match), "before": snapshot(before)}
        if connection_id is not None:
            notice["connection_id"] = int(connection_id)
        if existing_only:
            # A rename rewrites events already on a calendar. It never adds a match the calendar never had.
            notice["existing_only"] = True
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
            SELECT Id, MatchId, EventType, Payload, Attempts, CreatedAt
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
        cursor.execute("SELECT COUNT(*) AS Leased FROM tblNotificationOutbox WHERE Status = 'leased'")
        leased = int((cursor.fetchone() or {}).get("Leased") or 0)
        for cid in connection_ids:
            states.setdefault(int(cid), {"last_status": None, "last_detail": "", "last_at": None})
            states[int(cid)]["queue_pending"] = pending > 0
            # A notice is being written right now. The queue is shared, so this is the worker's state, not one connection's.
            states[int(cid)]["in_flight"] = leased > 0
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
                    NotifySyncProblem = %s, UpdatedAt = SYSUTCDATETIME()
                WHERE Id = %s
                """,
                (
                    row["p256dh"], row["auth"], row.get("admin_id"), row.get("season_id"), row.get("team_id"),
                    row.get("user_agent"),
                    int(row["notify_scheduled"]), int(row["notify_changed"]), int(row["notify_cancelled"]),
                    int(row["notify_live"]), int(row["notify_ended"]), int(row["notify_score"]),
                    int(row["notify_reminder"]), int(row.get("notify_sync_problem", True)),
                    int(existing["Id"]),
                ),
            )
            self.db.commit()
            return self.subscription_by_endpoint(row["endpoint"])
        cursor.execute(
            """
            INSERT INTO tblPushSubscriptions (
                Endpoint, EndpointHash, P256dh, AuthSecret, AdminId, SeasonId, TeamId, UserAgent,
                NotifyScheduled, NotifyChanged, NotifyCancelled, NotifyLive, NotifyEnded, NotifyScore, NotifyReminder,
                NotifySyncProblem
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                row["endpoint"], endpoint_hash(row["endpoint"]), row["p256dh"], row["auth"], row.get("admin_id"), row.get("season_id"),
                row.get("team_id"), row.get("user_agent"),
                int(row["notify_scheduled"]), int(row["notify_changed"]), int(row["notify_cancelled"]),
                int(row["notify_live"]), int(row["notify_ended"]), int(row["notify_score"]),
                int(row["notify_reminder"]), int(row.get("notify_sync_problem", True)),
            ),
        )
        self.db.commit()
        return self.subscription_by_endpoint(row["endpoint"])

    def subscription_by_endpoint(self, endpoint: str) -> dict | None:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT Id, Endpoint, P256dh, AuthSecret, AdminId, SeasonId, TeamId, Void, FailureCount, LastSuccessAt,
                   NotifyScheduled, NotifyChanged, NotifyCancelled, NotifyLive, NotifyEnded, NotifyScore, NotifyReminder,
                   NotifySyncProblem
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
                NotifyLive = %s, NotifyEnded = %s, NotifyScore = %s, NotifyReminder = %s, NotifySyncProblem = %s,
                SeasonId = %s, TeamId = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE EndpointHash = %s AND Void = 0
            """,
            (
                int(merged["notify_scheduled"]), int(merged["notify_changed"]), int(merged["notify_cancelled"]),
                int(merged["notify_live"]), int(merged["notify_ended"]), int(merged["notify_score"]),
                int(merged["notify_reminder"]), int(merged.get("notify_sync_problem", True)),
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

    def note_push_success(self, subscription_id: int) -> None:
        # Only consecutive failures can void a device, so one accepted message clears the count.
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE tblPushSubscriptions SET LastSuccessAt = SYSUTCDATETIME(), FailureCount = 0 WHERE Id = %s",
            (subscription_id,),
        )
        self.db.commit()

    def enqueue_sync_problem(self, connection_id: int, admin_id: int, provider: str) -> int | None:
        """Queue one push to the admin's own devices: this connection needs them to connect again.

        The outbox row needs a match id, and this notice has none, so it uses 0. Nothing reads a
        match for it. Its key is per connection, so a retry or a second revoke queues nothing new.
        """
        key = f"connection:{int(connection_id)}:sync_problem"
        payload = json.dumps(
            {"event": "sync_problem", "connection_id": int(connection_id), "admin_id": int(admin_id), "provider": provider},
            separators=(",", ":"),
        )
        cursor = self.db.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO tblNotificationOutbox (MatchId, EventType, IdempotencyKey, Payload)
                SELECT 0, %s, %s, %s
                WHERE NOT EXISTS (SELECT 1 FROM tblNotificationOutbox WHERE IdempotencyKey = %s)
                """,
                ("sync_problem", key, payload, key),
            )
        except pyodbc.IntegrityError:
            return None
        self.db.commit()
        return cursor.lastrowid

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
            SELECT Id, Endpoint, P256dh, AuthSecret, AdminId, SeasonId, TeamId, Void, FailureCount, LastSuccessAt,
                   NotifyScheduled, NotifyChanged, NotifyCancelled, NotifyLive, NotifyEnded, NotifyScore, NotifyReminder,
                   NotifySyncProblem
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
        return self.attach_rosters([self._row_notice(row) for row in cursor.fetchall()])

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

    def withdrawn_connection_ids(self, match_id: int) -> set[int]:
        """Connections the admin removed this match from. Ordinary calendar writes skip them."""
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            "SELECT ConnectionId FROM tblCalendarEventMaps WHERE MatchId = %s AND Withdrawn = 1",
            (int(match_id),),
        )
        return {int(row["ConnectionId"]) for row in cursor.fetchall()}

    def set_withdrawn(self, connection_id: int, match_id: int, withdrawn: bool) -> None:
        """Mark one match withdrawn (or active again) for one connection.

        The content hash is cleared on both changes, so a restore always writes the event
        and a withdrawal never matches a stored hash.
        """
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblCalendarEventMaps
            SET Withdrawn = %s, ContentHash = '', UpdatedAt = SYSUTCDATETIME()
            WHERE ConnectionId = %s AND MatchId = %s
            """,
            (1 if withdrawn else 0, int(connection_id), int(match_id)),
        )
        if cursor.rowcount == 0 and withdrawn:
            cursor.execute(
                """
                INSERT INTO tblCalendarEventMaps (ConnectionId, MatchId, ExternalEventId, ContentHash, Withdrawn)
                VALUES (%s, %s, '', '', 1)
                """,
                (int(connection_id), int(match_id)),
            )
        self.db.commit()

    def connection_match_states(self, admin_id: int, match_id: int) -> list[dict]:
        """For each of the admin's live connections: whether this match is on that calendar."""
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT c.Id, c.Provider, c.Status, m.Withdrawn, m.ContentHash, m.ExternalEventId
            FROM tblCalendarConnections c
            LEFT JOIN tblCalendarEventMaps m ON m.ConnectionId = c.Id AND m.MatchId = %s
            WHERE c.AdminId = %s AND c.Void = 0
            ORDER BY c.Provider
            """,
            (int(match_id), int(admin_id)),
        )
        states = []
        for row in cursor.fetchall():
            removed = bool(row["Withdrawn"]) if row["Withdrawn"] is not None else False
            synced = bool((row["ContentHash"] or "").strip()) and bool((row["ExternalEventId"] or "").strip())
            states.append(
                {
                    "id": int(row["Id"]),
                    "provider": row["Provider"],
                    "status": row["Status"],
                    "removed": removed,
                    "on_calendar": synced and not removed,
                }
            )
        return states

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
        lookup = self.db.cursor(dictionary=True)
        lookup.execute(
            "SELECT TOP 1 Id FROM tblCalendarConnections WHERE AdminId = %s AND Provider = %s ORDER BY Id DESC",
            (admin_id, provider),
        )
        previous = lookup.fetchone()
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
        if previous and connection_id is not None:
            # Reconnecting the same provider keeps the events already written and the matches the
            # admin removed. The hash is cleared, so the backfill rewrites each mapped event in place
            # rather than posting a second copy. A different account answers 404 and gets a new event.
            cursor.execute(
                """
                INSERT INTO tblCalendarEventMaps (ConnectionId, MatchId, ExternalEventId, ContentHash, Withdrawn)
                SELECT %s, MatchId, ExternalEventId, '', Withdrawn
                FROM tblCalendarEventMaps
                WHERE ConnectionId = %s
                """,
                (int(connection_id), int(previous["Id"])),
            )
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

    def entity_name(self, kind: str, entity_id: int) -> str | None:
        table = {"team": "tblTeams", "season": "tblSeasons"}[kind]
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(f"SELECT Name FROM dbo.{table} WHERE Id = %s", (int(entity_id),))
        row = cursor.fetchone()
        return row["Name"] if row else None

    def touch_matches_for_rename(self, team_id: int | None = None, season_id: int | None = None) -> list[dict]:
        """Bump UpdatedAt on every live match that shows a renamed team or season.

        The feed's SEQUENCE follows UpdatedAt, so this is what makes subscribed calendars
        pick up the new name. Returns the matches as notice rows, for one calendar write each.
        """
        if (team_id is None) == (season_id is None):
            raise ValueError("pass exactly one of team_id or season_id")
        if team_id is not None:
            where = "m.Void = 0 AND (m.Team1 = %s OR m.Team2 = %s)"
            params = (int(team_id), int(team_id))
        else:
            where = "m.Void = 0 AND m.SeasonId = %s"
            params = (int(season_id),)
        return self._touch_notices(where, params)

    def touch_matches_for_player(self, player_id: int, season_id: int | None = None) -> list[dict]:
        """Bump UpdatedAt on every live match the player's name appears in, in one season or in all.

        A match counts when the player is on a roster of a team in that match. Removed roster rows
        still count, so the match is touched after a player leaves a team as well. The name is
        read again at write time, so a touch never carries stale text.
        """
        where = (
            "m.Void = 0 AND EXISTS (SELECT 1 FROM tblPlayersSeasonsTeams pst "
            "WHERE pst.PlayerId = %s AND pst.SeasonId = m.SeasonId AND (pst.TeamId = m.Team1 OR pst.TeamId = m.Team2))"
        )
        params: tuple = (int(player_id),)
        if season_id is not None:
            where += " AND m.SeasonId = %s"
            params = (int(player_id), int(season_id))
        return self._touch_notices(where, params)

    def _touch_notices(self, where: str, params: tuple) -> list[dict]:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(f"UPDATE m SET UpdatedAt = SYSUTCDATETIME() FROM tblMatches AS m WHERE {where}", params)
        cursor.execute(
            f"""
            SELECT
                m.Id, m.SeasonId, s.Name AS SeasonName,
                m.Team1, m.Team2, t1.Name AS Team1Name, t2.Name AS Team2Name,
                m.ScheduledDate, m.Status, m.Void, m.Category, m.Outcome, m.NetPoints,
                m.UpdatedAt, m.StartedAt, m.CreatedAt
            FROM tblMatches m
            JOIN tblSeasons s ON s.Id = m.SeasonId
            LEFT JOIN tblTeams t1 ON t1.Id = m.Team1
            LEFT JOIN tblTeams t2 ON t2.Id = m.Team2
            WHERE {where}
            ORDER BY m.ScheduledDate, m.[Order], m.Id
            """,
            params,
        )
        rows = cursor.fetchall()
        self.db.commit()
        return [self._row_notice(row) for row in rows]

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
        # ContentHash is CHAR(64), so an empty hash comes back padded with spaces.
        return {"external_event_id": row["ExternalEventId"], "content_hash": (row["ContentHash"] or "").strip()}

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
        "last_success_at": row.get("LastSuccessAt"),
        "notify_scheduled": bool(row["NotifyScheduled"]),
        "notify_changed": bool(row["NotifyChanged"]),
        "notify_cancelled": bool(row["NotifyCancelled"]),
        "notify_live": bool(row["NotifyLive"]),
        "notify_ended": bool(row["NotifyEnded"]),
        "notify_score": bool(row["NotifyScore"]),
        "notify_reminder": bool(row["NotifyReminder"]),
        "notify_sync_problem": bool(row.get("NotifySyncProblem", True)),
    }


def public_subscription(row: dict) -> dict:
    hidden = dict(row)
    hidden.pop("endpoint", None)
    hidden.pop("p256dh", None)
    hidden.pop("auth", None)
    return hidden
