from datetime import datetime, timezone
import json
import socket
import unittest
from unittest.mock import patch

from tests import test_auth_service as fakes  # noqa: F401  (sets environment before app imports)

from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database import get_db
from core.permissions import MANAGE_APPS, MANAGE_WEBHOOKS
from core.security import create_access_token
from repositories.webhook_repository import MAX_ATTEMPTS
from routes import auth_routes, webhook_routes
from schemas.auth_schema import AdminCreateRequest
from services.auth_service import AuthService
from services.webhooks import enqueue as webhook_enqueue
from services.webhooks.events import classify_match_webhook_events
from services.webhooks.security import (
    WebhookTargetError,
    sign_webhook_payload,
    unseal_signing_secret,
    validate_webhook_url,
    verify_webhook_signature,
)
from services.webhooks.service import WebhookService
from services.webhooks.worker import deliver_item


class _FakeHttpResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class FakeWebhookRepository:
    def __init__(self):
        self._hook_seq = 0
        self._delivery_seq = 0
        self.hooks: dict[int, dict] = {}
        self.deliveries: dict[int, dict] = {}
        self._dedupe: set[tuple[int, str]] = set()

    def _copy_hook(self, row: dict) -> dict:
        copied = dict(row)
        copied["events"] = list(row["events"])
        return copied

    def list_all(self) -> list[dict]:
        return [
            self._copy_hook(row)
            for row in sorted(self.hooks.values(), key=lambda r: r["id"], reverse=True)
            if not row["void"]
        ]

    def get_by_id(self, webhook_id: int) -> dict | None:
        row = self.hooks.get(int(webhook_id))
        if not row or row["void"]:
            return None
        return self._copy_hook(row)

    def active_for_event(self, event_type: str) -> list[dict]:
        return [
            self._copy_hook(row)
            for row in self.hooks.values()
            if not row["void"] and row["is_active"] and event_type in row["events"]
        ]

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
    ) -> dict:
        self._hook_seq += 1
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        record = {
            "id": self._hook_seq,
            "name": name,
            "url": url,
            "description": description,
            "secret_ciphertext": secret_ciphertext,
            "secret_hint": secret_hint,
            "events": list(events),
            "is_active": bool(is_active),
            "void": False,
            "failure_count": 0,
            "last_delivered_at": None,
            "last_status_code": None,
            "last_error": None,
            "created_at": now,
            "updated_at": now,
            "created_by": int(created_by),
        }
        self.hooks[self._hook_seq] = record
        return self._copy_hook(record)

    def update(self, webhook_id: int, changes: dict) -> dict | None:
        row = self.hooks.get(int(webhook_id))
        if not row or row["void"]:
            return None
        for field in ("name", "url", "description", "is_active"):
            if field in changes:
                row[field] = changes[field]
        if "events" in changes and changes["events"] is not None:
            row["events"] = list(changes["events"])
        row["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        return self._copy_hook(row)

    def rotate_secret(self, webhook_id: int, secret_ciphertext: str, secret_hint: str) -> dict | None:
        row = self.hooks.get(int(webhook_id))
        if not row or row["void"]:
            return None
        row["secret_ciphertext"] = secret_ciphertext
        row["secret_hint"] = secret_hint
        row["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        return self._copy_hook(row)

    def delete(self, webhook_id: int) -> bool:
        row = self.hooks.get(int(webhook_id))
        if not row or row["void"]:
            return False
        row["void"] = True
        row["is_active"] = False
        row["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
        return True

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
        pair = (int(webhook_id), idempotency_key)
        if pair in self._dedupe:
            return None
        self._dedupe.add(pair)
        self._delivery_seq += 1
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.deliveries[self._delivery_seq] = {
            "id": self._delivery_seq,
            "webhook_id": int(webhook_id),
            "event_id": event_id,
            "event_type": event_type,
            "payload": payload,
            "status": status,
            "attempts": attempts,
            "response_status": None,
            "duration_ms": None,
            "error_detail": None,
            "created_at": now,
            "delivered_at": None,
        }
        return self._delivery_seq

    def mark_sent(self, delivery_id: int, webhook_id: int, status_code: int, duration_ms: int) -> None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        d = self.deliveries[int(delivery_id)]
        d["status"] = "sent"
        d["response_status"] = int(status_code)
        d["duration_ms"] = int(duration_ms)
        d["error_detail"] = None
        d["delivered_at"] = now
        h = self.hooks[int(webhook_id)]
        h["failure_count"] = 0
        h["last_delivered_at"] = now
        h["last_status_code"] = int(status_code)
        h["last_error"] = None

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
        dead = terminal or attempts >= MAX_ATTEMPTS
        d = self.deliveries[int(delivery_id)]
        d["status"] = "dead" if dead else "pending"
        d["response_status"] = status_code
        d["duration_ms"] = duration_ms
        d["error_detail"] = error_detail
        h = self.hooks[int(webhook_id)]
        h["failure_count"] = int(h["failure_count"]) + 1
        h["last_status_code"] = status_code
        h["last_error"] = error_detail

    def list_deliveries(self, webhook_id: int, limit: int = 25) -> list[dict]:
        items = [
            dict(row)
            for row in self.deliveries.values()
            if row["webhook_id"] == int(webhook_id)
        ]
        items.sort(key=lambda r: r["id"], reverse=True)
        return items[:limit]


class _FakeDbHandle:
    def cursor(self):
        return None


class WebhookSecurityAndSsrfTests(unittest.TestCase):
    def test_validate_webhook_url_enforces_https_and_blocks_private_targets_in_prod(self):
        self.assertEqual(
            validate_webhook_url("https://hooks.example.com/cpl/events", app_env="prod"),
            "https://hooks.example.com/cpl/events",
        )
        for bad in (
            "http://hooks.example.com/cpl",
            "https://localhost/hook",
            "https://internal.local/hook",
            "https://service.internal/hook",
            "https://127.0.0.1/hook",
            "https://10.0.0.8/hook",
            "https://192.168.1.20/hook",
            "https://169.254.169.254/latest/meta-data",
            "https://user:secret@hooks.example.com/hook",
            "",
        ):
            with self.subTest(url=bad):
                with self.assertRaises(WebhookTargetError):
                    validate_webhook_url(bad, app_env="prod")

    def test_validate_webhook_url_blocks_dns_rebinding_to_private_ip(self):
        fake_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("10.24.0.5", 443))]
        with patch("services.webhooks.security.socket.getaddrinfo", return_value=fake_addrinfo):
            with self.assertRaises(WebhookTargetError):
                validate_webhook_url("https://rebind.example.com/hook", resolve_dns=True, app_env="prod")

        public_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        with patch("services.webhooks.security.socket.getaddrinfo", return_value=public_addrinfo):
            self.assertEqual(
                validate_webhook_url("https://hooks.example.com/hook", resolve_dns=True, app_env="prod"),
                "https://hooks.example.com/hook",
            )

    def test_validate_webhook_url_allows_localhost_http_only_in_dev(self):
        self.assertEqual(
            validate_webhook_url("http://localhost:8787/webhook", app_env="dev"),
            "http://localhost:8787/webhook",
        )
        with self.assertRaises(WebhookTargetError):
            validate_webhook_url("http://hooks.example.com/webhook", app_env="dev")

    def test_hmac_signature_verifies_and_detects_tampering(self):
        secret = "whsec_test_secret_1234567890"
        body = '{"event":"match.ended","data":{"id":42}}'
        sig = sign_webhook_payload(secret, 1700000000, body)
        self.assertTrue(sig.startswith("sha256="))
        self.assertTrue(verify_webhook_signature(secret, 1700000000, body, sig))
        self.assertFalse(verify_webhook_signature(secret, 1700000001, body, sig))
        self.assertFalse(verify_webhook_signature(secret, 1700000000, body + " ", sig))
        self.assertFalse(verify_webhook_signature("whsec_other", 1700000000, body, sig))


class WebhookRoutesAndDeliveryTests(unittest.TestCase):
    def setUp(self):
        admins = fakes.FakeAdminRepository()
        perms = fakes.FakePermissionRepository(admins)
        self.auth_service = AuthService(admins, perms)
        self.owner = self.auth_service.bootstrap_admin(
            AdminCreateRequest(username="owner", password="correct-horse-battery")
        )
        self.sent_requests: list[tuple[str, bytes, dict]] = []
        self.next_status = 200

        def fake_post(url: str, content: bytes, headers: dict):
            self.sent_requests.append((url, content, headers))
            return _FakeHttpResponse(self.next_status)

        self.repo = FakeWebhookRepository()
        self.service = WebhookService(self.repo, post=fake_post)

        app = FastAPI()
        app.state.webhook_service = self.service
        app.dependency_overrides[auth_routes.get_service] = lambda: self.auth_service
        app.dependency_overrides[get_db] = lambda: iter([None])
        app.include_router(auth_routes.router)
        app.include_router(webhook_routes.router)
        self.client = TestClient(app)
        self._phones = 9500000100

    def _bearer(self, admin) -> dict[str, str]:
        record = self.auth_service.repository.records[admin.id]
        token = create_access_token(admin.id, admin.username, record.get("TokenVersion", 0))
        return {"Authorization": f"Bearer {token}"}

    def _make_admin(self, username: str, permissions: list[str]):
        self._phones += 1
        created = self.auth_service.create_admin(
            AdminCreateRequest(
                username=username,
                password="correct-horse-battery",
                permissions=permissions,
                email=f"{username}@example.com",
                phone_code="+91",
                phone_number=str(self._phones),
            ),
            self.owner,
        )
        self.auth_service.repository.records[created.id]["IsActive"] = True
        self.auth_service.repository.records[created.id]["MustChangePassword"] = False
        return created

    def test_webhook_routes_require_webhooks_manage_permission(self):
        apps_only = self._make_admin("apps.only", ["settings.update", MANAGE_APPS])
        headers = self._bearer(apps_only)

        self.assertEqual(self.client.get("/webhooks", headers=headers).status_code, 403)
        self.assertEqual(
            self.client.post(
                "/webhooks",
                json={"name": "Hook", "url": "https://hooks.example.com/cpl", "events": ["match.ended"]},
                headers=headers,
            ).status_code,
            403,
        )
        self.assertEqual(self.client.patch("/webhooks/1", json={"is_active": False}, headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/webhooks/1/rotate-secret", headers=headers).status_code, 403)
        self.assertEqual(self.client.post("/webhooks/1/test", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/webhooks/1/deliveries", headers=headers).status_code, 403)
        self.assertEqual(self.client.delete("/webhooks/1", headers=headers).status_code, 403)

    def test_secret_sealing_one_time_reveal_test_ping_and_deliveries_log(self):
        manager = self._make_admin("hook.admin", [MANAGE_WEBHOOKS])
        headers = self._bearer(manager)

        created_res = self.client.post(
            "/webhooks",
            json={
                "name": "Discord Match Feed",
                "url": "https://hooks.example.com/discord",
                "events": ["match.live", "match.ended"],
            },
            headers=headers,
        )
        self.assertEqual(created_res.status_code, 201)
        created = created_res.json()["data"]
        raw_secret = created["signing_secret"]
        self.assertTrue(raw_secret.startswith("whsec_"))

        # Stored secret is AES-GCM sealed, never plaintext
        stored = self.repo.hooks[created["id"]]
        self.assertNotEqual(stored["secret_ciphertext"], raw_secret)
        self.assertEqual(unseal_signing_secret(stored["secret_ciphertext"]), raw_secret)

        # GET /webhooks returns secret_hint only
        listed = self.client.get("/webhooks", headers=headers).json()["data"]["webhooks"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["secret_hint"], created["secret_hint"])
        self.assertNotIn("signing_secret", listed[0])
        self.assertNotIn("secret_ciphertext", listed[0])

        # Synchronous test ping signs payload with X-CPL-Signature-256 and records delivery
        public_ip = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]
        with patch("services.webhooks.security.socket.getaddrinfo", return_value=public_ip):
            ping = self.client.post(f"/webhooks/{created['id']}/test", headers=headers)
        self.assertEqual(ping.status_code, 200)
        self.assertTrue(ping.json()["data"]["ok"])
        self.assertEqual(ping.json()["data"]["status_code"], 200)

        self.assertEqual(len(self.sent_requests), 1)
        sent_url, sent_bytes, sent_headers = self.sent_requests[0]
        self.assertEqual(sent_url, "https://hooks.example.com/discord")
        self.assertEqual(sent_headers["X-CPL-Event"], "webhook.test")
        self.assertTrue(
            verify_webhook_signature(
                raw_secret,
                int(sent_headers["X-CPL-Timestamp"]),
                sent_bytes.decode("utf-8"),
                sent_headers["X-CPL-Signature-256"],
            )
        )

        deliveries = self.client.get(f"/webhooks/{created['id']}/deliveries", headers=headers).json()["data"]
        self.assertEqual(len(deliveries), 1)
        self.assertEqual(deliveries[0]["status"], "sent")
        self.assertEqual(deliveries[0]["response_status"], 200)

        # Rotate secret replaces ciphertext
        rotated = self.client.post(f"/webhooks/{created['id']}/rotate-secret", headers=headers).json()["data"]
        self.assertNotEqual(rotated["signing_secret"], raw_secret)
        self.assertEqual(unseal_signing_secret(self.repo.hooks[created["id"]]["secret_ciphertext"]), rotated["signing_secret"])

    def test_worker_retry_terminal_4xx_and_inactive_handling(self):
        headers = self._bearer(self.owner)
        created = self.client.post(
            "/webhooks",
            json={"name": "Worker Target", "url": "https://hooks.example.com/worker", "events": ["team.created"]},
            headers=headers,
        ).json()["data"]
        hook = self.repo.hooks[created["id"]]
        public_ip = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 443))]

        # 502 keeps delivery pending for exponential retry until MAX_ATTEMPTS
        d1 = self.repo.enqueue_delivery(
            webhook_id=hook["id"],
            event_id="evt_1",
            event_type="team.created",
            idempotency_key="team:1",
            payload='{"event":"team.created"}',
            status="leased",
            attempts=1,
        )
        with patch("services.webhooks.security.socket.getaddrinfo", return_value=public_ip):
            deliver_item(
                self.repo,
                {
                    "Id": d1,
                    "WebhookId": hook["id"],
                    "EventId": "evt_1",
                    "EventType": "team.created",
                    "Payload": '{"event":"team.created"}',
                    "Attempts": 1,
                    "Url": hook["url"],
                    "SecretCiphertext": hook["secret_ciphertext"],
                    "IsActive": True,
                    "Void": False,
                },
                post=lambda *_args: _FakeHttpResponse(502),
            )
        self.assertEqual(self.repo.deliveries[d1]["status"], "pending")
        self.assertEqual(self.repo.hooks[hook["id"]]["failure_count"], 1)

        # 404 is terminal immediately (status = dead)
        d2 = self.repo.enqueue_delivery(
            webhook_id=hook["id"],
            event_id="evt_2",
            event_type="team.created",
            idempotency_key="team:2",
            payload='{"event":"team.created"}',
            status="leased",
            attempts=1,
        )
        with patch("services.webhooks.security.socket.getaddrinfo", return_value=public_ip):
            deliver_item(
                self.repo,
                {
                    "Id": d2,
                    "WebhookId": hook["id"],
                    "EventId": "evt_2",
                    "EventType": "team.created",
                    "Payload": '{"event":"team.created"}',
                    "Attempts": 1,
                    "Url": hook["url"],
                    "SecretCiphertext": hook["secret_ciphertext"],
                    "IsActive": True,
                    "Void": False,
                },
                post=lambda *_args: _FakeHttpResponse(404),
            )
        self.assertEqual(self.repo.deliveries[d2]["status"], "dead")

        # Inactive webhook marks delivery dead without calling network sender
        called = []
        d3 = self.repo.enqueue_delivery(
            webhook_id=hook["id"],
            event_id="evt_3",
            event_type="team.created",
            idempotency_key="team:3",
            payload='{"event":"team.created"}',
            status="leased",
            attempts=1,
        )
        deliver_item(
            self.repo,
            {
                "Id": d3,
                "WebhookId": hook["id"],
                "EventId": "evt_3",
                "EventType": "team.created",
                "Payload": '{"event":"team.created"}',
                "Attempts": 1,
                "Url": hook["url"],
                "SecretCiphertext": hook["secret_ciphertext"],
                "IsActive": False,
                "Void": False,
            },
            post=lambda *args: called.append(args),
        )
        self.assertEqual(called, [])
        self.assertEqual(self.repo.deliveries[d3]["status"], "dead")

    def test_event_fanout_and_match_classification_deduplicate_by_idempotency_key(self):
        headers = self._bearer(self.owner)
        h_match = self.client.post(
            "/webhooks",
            json={"name": "Match Hook", "url": "https://hooks.example.com/m", "events": ["match.live", "match.ended"]},
            headers=headers,
        ).json()["data"]
        self.client.post(
            "/webhooks",
            json={"name": "Team Hook", "url": "https://hooks.example.com/t", "events": ["team.created"]},
            headers=headers,
        )

        # Match classification (STATUS_SCHEDULED = 1, STATUS_LIVE = 2)
        before = {"id": 9, "status": 1, "scheduled_date": "2026-11-21", "updated_at": "t1"}
        live = {"id": 9, "status": 2, "scheduled_date": "2026-11-21", "updated_at": "t2"}
        self.assertEqual(classify_match_webhook_events("updated", before, live), ["match.live"])

        with patch.object(webhook_enqueue, "WebhookRepository", return_value=self.repo):
            queued = webhook_enqueue.publish_match_webhooks(_FakeDbHandle(), "updated", before, live)
            duplicate = webhook_enqueue.publish_match_webhooks(_FakeDbHandle(), "updated", before, live)

        self.assertEqual(queued, 1)
        self.assertEqual(duplicate, 0)
        only_delivery = next(iter(self.repo.deliveries.values()))
        self.assertEqual(only_delivery["webhook_id"], h_match["id"])
        envelope = json.loads(only_delivery["payload"])
        self.assertEqual(envelope["event"], "match.live")
        self.assertEqual(envelope["data"]["match"]["id"], 9)


if __name__ == "__main__":
    unittest.main()
