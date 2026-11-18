"""Web Push send path, end to end, against a local HTTP server.

No browser and no push service are contacted. A local server stands in for the
push service. The test checks what a real push service would check: the VAPID
authorization is a valid ES256 signature by the advertised public key, the body
is encrypted for the subscriber (aes128gcm), and the decrypted payload is the
minimal notice. Status codes map to gone, blocked, and retryable as documented.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from tests import test_auth_service as _env  # noqa: F401  (sets required app env before config imports)

os.environ.setdefault("CPL_PUSH_ENABLED", "false")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _Recorder(BaseHTTPRequestHandler):
    status = 201
    seen: list = []

    def do_POST(self):  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        self.send_response(type(self).status)
        self.end_headers()

    def log_message(self, *args):  # keep test output quiet
        pass


class PushSendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        cls.vapid_key = ec.generate_private_key(ec.SECP256R1())
        cls.vapid_private = _b64url(cls.vapid_key.private_numbers().private_value.to_bytes(32, "big"))
        cls.vapid_public = _b64url(
            cls.vapid_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        )
        cls.subscriber_key = ec.generate_private_key(ec.SECP256R1())
        cls.subscriber_public = _b64url(
            cls.subscriber_key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        )
        cls.auth_secret = os.urandom(16)
        _Recorder.seen = []
        cls.server = HTTPServer(("127.0.0.1", 0), _Recorder)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Recorder.seen = []
        _Recorder.status = 201

    def _subscription(self, name="device-1"):
        return {
            "id": 1,
            "endpoint": f"{self.base}/push/{name}",
            "p256dh": self.subscriber_public,
            "auth": _b64url(self.auth_secret),
        }

    def _send(self, payload, subscription=None):
        from services.notify.push import send_web_push

        return send_web_push(
            subscription or self._subscription(),
            payload,
            private_key=self.vapid_private,
            subject="mailto:cpl-test@example.com",
        )

    def _verify_vapid(self, authorization: str) -> None:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

        self.assertTrue(authorization.startswith("vapid "), authorization)
        parts = dict(item.strip().split("=", 1) for item in authorization[len("vapid "):].split(","))
        self.assertEqual(parts["k"], self.vapid_public)
        token = parts["t"]
        header, claims, signature = token.split(".")
        self.assertEqual(json.loads(_unb64url(header))["alg"], "ES256")
        claim_set = json.loads(_unb64url(claims))
        self.assertEqual(claim_set["sub"], "mailto:cpl-test@example.com")
        self.assertEqual(claim_set["aud"], self.base)
        import time

        # A push service rejects a token that expired; the lifetime must be short and in the future.
        self.assertGreater(claim_set["exp"], time.time())
        self.assertLess(claim_set["exp"], time.time() + 24 * 3600)
        raw = _unb64url(signature)
        self.assertEqual(len(raw), 64)
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), _unb64url(self.vapid_public))
        try:
            public.verify(der, f"{header}.{claims}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
        except InvalidSignature:
            self.fail("VAPID token is not signed by the advertised key")

    def _decrypt(self, body: bytes) -> dict:
        import http_ece

        plain = http_ece.decrypt(
            body,
            # With private_key set and no dh, the library takes the sender's ephemeral key from the header.
            private_key=self.subscriber_key,
            auth_secret=self.auth_secret,
            version="aes128gcm",
        )
        return json.loads(plain)

    def test_a_delivered_notice_is_signed_encrypted_and_minimal(self):
        from services.notify.push import push_payload

        payload = push_payload("live", {"id": 12, "season_id": 3, "team1_name": "Red", "team2_name": "Blue",
                                        "scheduled_date": None, "status": 2}, None, "https://league.example", notice_id=41)
        result = self._send(payload)
        self.assertEqual(result, {"ok": True, "blocked": False, "gone": False, "reason": ""})

        self.assertEqual(len(_Recorder.seen), 1)
        request = _Recorder.seen[0]
        self.assertEqual(request["path"], "/push/device-1")
        headers = {key.lower(): value for key, value in request["headers"].items()}
        self.assertEqual(headers["content-encoding"], "aes128gcm")
        self._verify_vapid(headers["authorization"])
        self.assertTrue(headers.get("ttl"))

        delivered = self._decrypt(request["body"])
        self.assertEqual(delivered, payload)
        # Minimal: no subscription data, no secrets, and the notice identifies the match only.
        text = json.dumps(delivered)
        for secret in (self._subscription()["endpoint"], self.vapid_private, _b64url(self.auth_secret), self.subscriber_public):
            self.assertNotIn(secret, text)

    def test_a_gone_or_unknown_endpoint_is_voided_not_retried(self):
        from services.notify.push import push_payload

        payload = push_payload("scheduled", {"id": 12, "season_id": 3, "team1_name": "Red", "team2_name": "Blue",
                                             "scheduled_date": None, "status": 1}, None, self.base)
        for status in (404, 410):
            _Recorder.status = status
            result = self._send(payload, self._subscription(f"gone-{status}"))
            self.assertFalse(result["ok"])
            self.assertTrue(result["gone"], status)

    def test_a_rate_limit_or_outage_is_retryable_and_keeps_the_subscription(self):
        from services.notify.push import push_payload

        payload = push_payload("scheduled", {"id": 12, "season_id": 3, "team1_name": "Red", "team2_name": "Blue",
                                             "scheduled_date": None, "status": 1}, None, self.base)
        for status in (429, 503):
            _Recorder.status = status
            result = self._send(payload, self._subscription(f"busy-{status}"))
            self.assertFalse(result["ok"])
            self.assertFalse(result["gone"])
            self.assertFalse(result["blocked"])

    def test_an_unreachable_push_service_fails_without_leaking_the_endpoint(self):
        import logging

        from services.notify.push import push_payload

        payload = push_payload("scheduled", {"id": 12, "season_id": 3, "team1_name": "Red", "team2_name": "Blue",
                                             "scheduled_date": None, "status": 1}, None, self.base)
        dead = {"id": 9, "endpoint": "http://127.0.0.1:1/push/secret-path", "p256dh": self.subscriber_public,
                "auth": _b64url(self.auth_secret)}
        with self.assertLogs("services.notify.push", level=logging.INFO) as logs:
            result = self._send(payload, dead)
        self.assertFalse(result["ok"])
        self.assertFalse(result["gone"])
        self.assertNotIn("secret-path", " ".join(logs.output + [result["reason"]]))

    def test_a_vapid_key_in_the_wrong_format_is_refused_before_any_request(self):
        from services.notify.push import push_payload, send_web_push

        payload = push_payload("scheduled", {"id": 12, "season_id": 3, "team1_name": "Red", "team2_name": "Blue",
                                             "scheduled_date": None, "status": 1}, None, self.base)
        result = send_web_push(self._subscription("bad-key"), payload, private_key="not-a-key", subject="mailto:a@b.c")
        self.assertFalse(result["ok"])
        self.assertEqual(_Recorder.seen, [])


if __name__ == "__main__":
    unittest.main()
