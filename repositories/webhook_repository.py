from __future__ import annotations

import json

from services.webhooks.events import ALL_WEBHOOK_EVENTS, ordered_webhook_events

MAX_ATTEMPTS = 5


def retry_delay_seconds(attempts: int) -> int:
    # 15s, 60s, 300s, 900s
    schedule = (15, 60, 300, 900)
    idx = max(0, min(int(attempts) - 1, len(schedule) - 1))
    return schedule[idx]


class WebhookRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def _as_bool(value) -> bool:
        if isinstance(value, (bytes, bytearray)):
            return bool(value[0]) if value else False
        return bool(value)

    @staticmethod
    def _parse_events(raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = [part.strip() for part in str(raw).split(",") if part.strip()]
        if not isinstance(parsed, list):
            return []
        return ordered_webhook_events(c for c in parsed if isinstance(c, str) and c in ALL_WEBHOOK_EVENTS)

    def _row_to_dict(self, row: dict) -> dict:
        return {
            "id": int(row["Id"]),
            "name": row["Name"],
            "url": row["Url"],
            "description": row.get("Description"),
            "secret_ciphertext": row["SecretCiphertext"],
            "secret_hint": row["SecretHint"],
            "events": self._parse_events(row.get("Events")),
            "is_active": self._as_bool(row["IsActive"]),
            "void": self._as_bool(row.get("Void", 0)),
            "failure_count": int(row.get("FailureCount") or 0),
            "last_delivered_at": row.get("LastDeliveredAt"),
            "last_status_code": row.get("LastStatusCode"),
            "last_error": row.get("LastError"),
            "created_at": row.get("CreatedAt"),
            "updated_at": row.get("UpdatedAt"),
            "created_by": int(row["CreatedBy"]),
            "auth_type": row.get("AuthType") or "none",
            "auth_header_name": row.get("AuthHeaderName"),
            "auth_ciphertext": row.get("AuthCiphertext"),
            "auth_hint": row.get("AuthHint"),
        }

    @staticmethod
    def _delivery_to_dict(row: dict) -> dict:
        return {
            "id": int(row["Id"]),
            "webhook_id": int(row["WebhookId"]),
            "event_id": row["EventId"],
            "event_type": row["EventType"],
            "idempotency_key": row.get("IdempotencyKey"),
            "payload": row.get("Payload"),
            "status": row["Status"],
            "attempts": int(row.get("Attempts") or 0),
            "response_status": row.get("ResponseStatus"),
            "duration_ms": row.get("DurationMs"),
            "error_detail": row.get("ErrorDetail"),
            "created_at": row.get("CreatedAt"),
            "delivered_at": row.get("DeliveredAt"),
        }

    def list_all(self) -> list[dict]:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Name, Url, Description, SecretCiphertext, SecretHint, Events,
                   IsActive, Void, FailureCount, LastDeliveredAt, LastStatusCode, LastError,
                   CreatedAt, UpdatedAt, CreatedBy, AuthType, AuthHeaderName, AuthCiphertext, AuthHint
            FROM tblWebhooks
            WHERE Void = 0
            ORDER BY Id DESC
            """
        )
        return [self._row_to_dict(row) for row in cursor.fetchall()]

    def active_for_event(self, event_type: str) -> list[dict]:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Name, Url, Description, SecretCiphertext, SecretHint, Events,
                   IsActive, Void, FailureCount, LastDeliveredAt, LastStatusCode, LastError,
                   CreatedAt, UpdatedAt, CreatedBy, AuthType, AuthHeaderName, AuthCiphertext, AuthHint
            FROM tblWebhooks
            WHERE Void = 0 AND IsActive = 1
            ORDER BY Id ASC
            """
        )
        items = [self._row_to_dict(row) for row in cursor.fetchall()]
        return [item for item in items if event_type in item["events"]]

    def get_by_id(self, webhook_id: int) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            SELECT Id, Name, Url, Description, SecretCiphertext, SecretHint, Events,
                   IsActive, Void, FailureCount, LastDeliveredAt, LastStatusCode, LastError,
                   CreatedAt, UpdatedAt, CreatedBy, AuthType, AuthHeaderName, AuthCiphertext, AuthHint
            FROM tblWebhooks
            WHERE Id = %s AND Void = 0
            """,
            (webhook_id,),
        )
        row = cursor.fetchone()
        return self._row_to_dict(row) if row else None

    def create(
        self,
        *,
        name: str,
        url: str,
        description: str | None,
        secret_ciphertext: str,
        secret_hint: str,
        events: list[str],
        is_active: bool,
        created_by: int,
        auth_type: str = "none",
        auth_header_name: str | None = None,
        auth_ciphertext: str | None = None,
        auth_hint: str | None = None,
    ) -> dict:
        cursor = self.db.cursor()
        events_json = json.dumps(ordered_webhook_events(events), separators=(",", ":"))
        cursor.execute(
            """
            INSERT INTO tblWebhooks (Name, Url, Description, SecretCiphertext, SecretHint, Events, IsActive, CreatedBy,
                                     AuthType, AuthHeaderName, AuthCiphertext, AuthHint)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                name,
                url,
                description,
                secret_ciphertext,
                secret_hint,
                events_json,
                1 if is_active else 0,
                created_by,
                auth_type,
                auth_header_name,
                auth_ciphertext,
                auth_hint,
            ),
        )
        webhook_id = int(cursor.lastrowid)
        self.db.commit()
        return self.get_by_id(webhook_id)  # type: ignore[return-value]

    def update(self, webhook_id: int, changes: dict) -> dict | None:
        cursor = self.db.cursor()
        assignments: list[str] = []
        values: list = []
        if "name" in changes:
            assignments.append("Name = %s")
            values.append(changes["name"])
        if "url" in changes:
            assignments.append("Url = %s")
            values.append(changes["url"])
        if "description" in changes:
            assignments.append("Description = %s")
            values.append(changes["description"])
        if "events" in changes:
            assignments.append("Events = %s")
            values.append(json.dumps(ordered_webhook_events(changes["events"]), separators=(",", ":")))
        if "is_active" in changes:
            assignments.append("IsActive = %s")
            values.append(1 if changes["is_active"] else 0)
        if "auth_type" in changes:
            assignments.append("AuthType = %s")
            values.append(changes["auth_type"])
            assignments.append("AuthHeaderName = %s")
            values.append(changes.get("auth_header_name"))
            assignments.append("AuthCiphertext = %s")
            values.append(changes.get("auth_ciphertext"))
            assignments.append("AuthHint = %s")
            values.append(changes.get("auth_hint"))
        if not assignments:
            return self.get_by_id(webhook_id)
        assignments.append("UpdatedAt = SYSUTCDATETIME()")
        values.append(webhook_id)
        cursor.execute(
            f"UPDATE tblWebhooks SET {', '.join(assignments)} WHERE Id = %s AND Void = 0",
            tuple(values),
        )
        if cursor.rowcount == 0:
            return None
        self.db.commit()
        return self.get_by_id(webhook_id)

    def rotate_secret(self, webhook_id: int, secret_ciphertext: str, secret_hint: str) -> dict | None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblWebhooks
            SET SecretCiphertext = %s, SecretHint = %s, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Void = 0
            """,
            (secret_ciphertext, secret_hint, webhook_id),
        )
        if cursor.rowcount == 0:
            return None
        self.db.commit()
        return self.get_by_id(webhook_id)

    def delete(self, webhook_id: int) -> bool:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblWebhooks
            SET Void = 1, IsActive = 0, UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s AND Void = 0
            """,
            (webhook_id,),
        )
        changed = cursor.rowcount > 0
        self.db.commit()
        return changed

    def enqueue_delivery(
        self,
        *,
        webhook_id: int,
        event_id: str,
        event_type: str,
        idempotency_key: str,
        payload: str,
        status: str = "pending",
        attempts: int = 0,
    ) -> int | None:
        cursor = self.db.cursor()
        key = idempotency_key[:200]
        cursor.execute(
            """
            SELECT TOP (1) Id FROM tblWebhookDeliveries
            WHERE WebhookId = %s AND IdempotencyKey = %s
            """,
            (webhook_id, key),
        )
        if cursor.fetchone() is not None:
            return None
        cursor.execute(
            """
            INSERT INTO tblWebhookDeliveries (WebhookId, EventId, EventType, IdempotencyKey, Payload, Status, Attempts)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (webhook_id, event_id[:64], event_type[:64], key, payload, status, attempts),
        )
        delivery_id = int(cursor.lastrowid)
        self.db.commit()
        return delivery_id

    def list_deliveries(self, webhook_id: int, limit: int = 25) -> list[dict]:
        cursor = self.db.cursor()
        cap = max(1, min(int(limit), 100))
        cursor.execute(
            f"""
            SELECT TOP ({cap}) Id, WebhookId, EventId, EventType, IdempotencyKey, Payload,
                   Status, Attempts, ResponseStatus, DurationMs, ErrorDetail, CreatedAt, DeliveredAt
            FROM tblWebhookDeliveries
            WHERE WebhookId = %s
            ORDER BY Id DESC
            """,
            (webhook_id,),
        )
        return [self._delivery_to_dict(row) for row in cursor.fetchall()]

    def claim_pending(self, limit: int = 20) -> list[dict]:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblWebhookDeliveries
            SET Status = 'dead', ErrorDetail = 'lease expired after the last attempt'
            WHERE Status = 'leased'
              AND AvailableAt <= CAST(SYSUTCDATETIME() AS DATETIME2(6))
              AND Attempts >= %s
            """,
            (MAX_ATTEMPTS,),
        )
        cursor.execute(
            f"""
            SELECT TOP ({max(1, min(int(limit), 50))}) Id
            FROM tblWebhookDeliveries
            WHERE Status IN ('pending', 'leased')
              AND AvailableAt <= CAST(SYSUTCDATETIME() AS DATETIME2(6))
              AND Attempts < %s
            ORDER BY Id ASC
            """,
            (MAX_ATTEMPTS,),
        )
        ids = [int(row["Id"]) for row in cursor.fetchall()]
        claimed: list[int] = []
        for item_id in ids:
            cursor.execute(
                """
                UPDATE tblWebhookDeliveries
                SET Status = 'leased',
                    Attempts = Attempts + 1,
                    AvailableAt = DATEADD(MINUTE, 2, SYSUTCDATETIME())
                WHERE Id = %s
                  AND Status IN ('pending', 'leased')
                  AND AvailableAt <= CAST(SYSUTCDATETIME() AS DATETIME2(6))
                  AND Attempts < %s
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
            SELECT d.Id, d.WebhookId, d.EventId, d.EventType, d.IdempotencyKey, d.Payload,
                   d.Status, d.Attempts, d.CreatedAt,
                   w.Url, w.SecretCiphertext, w.IsActive, w.Void,
                   w.AuthType, w.AuthHeaderName, w.AuthCiphertext
            FROM tblWebhookDeliveries d
            JOIN tblWebhooks w ON w.Id = d.WebhookId
            WHERE d.Id IN ({placeholders})
            ORDER BY d.Id ASC
            """,
            tuple(claimed),
        )
        return cursor.fetchall()

    def mark_sent(self, delivery_id: int, webhook_id: int, status_code: int, duration_ms: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblWebhookDeliveries
            SET Status = 'sent',
                ResponseStatus = %s,
                DurationMs = %s,
                ErrorDetail = NULL,
                DeliveredAt = SYSUTCDATETIME()
            WHERE Id = %s
            """,
            (status_code, duration_ms, delivery_id),
        )
        cursor.execute(
            """
            UPDATE tblWebhooks
            SET FailureCount = 0,
                LastDeliveredAt = SYSUTCDATETIME(),
                LastStatusCode = %s,
                LastError = NULL,
                UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s
            """,
            (status_code, webhook_id),
        )
        self.db.commit()

    def mark_failed(
        self,
        delivery_id: int,
        webhook_id: int,
        *,
        attempts: int,
        error_detail: str,
        status_code: int | None = None,
        duration_ms: int | None = None,
        terminal: bool = False,
    ) -> None:
        exhausted = terminal or attempts >= MAX_ATTEMPTS
        next_status = "dead" if exhausted else "pending"
        delay = retry_delay_seconds(attempts)
        detail = (error_detail or "Delivery failed.")[:400]
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblWebhookDeliveries
            SET Status = %s,
                AvailableAt = DATEADD(SECOND, %s, SYSUTCDATETIME()),
                ResponseStatus = %s,
                DurationMs = %s,
                ErrorDetail = %s
            WHERE Id = %s
            """,
            (next_status, delay, status_code, duration_ms, detail, delivery_id),
        )
        cursor.execute(
            """
            UPDATE tblWebhooks
            SET FailureCount = FailureCount + 1,
                LastStatusCode = %s,
                LastError = %s,
                UpdatedAt = SYSUTCDATETIME()
            WHERE Id = %s
            """,
            (status_code, detail, webhook_id),
        )
        self.db.commit()
