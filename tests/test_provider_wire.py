"""Calendar provider HTTP, over the wire.

The production request helpers (httpx, form encoding, JSON bodies, status
handling) are used unchanged. Only the host is redirected to a local server, so
the test sees the exact method, path, headers, and body that Google or Microsoft
would receive, and answers with the status codes their documented responses use.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

from tests import test_auth_service as _env  # noqa: F401  (sets required app env before config imports)

os.environ.setdefault("CPL_GOOGLE_CALENDAR_ENABLED", "false")

HOSTS = (
    "https://oauth2.googleapis.com",
    "https://www.googleapis.com",
    "https://login.microsoftonline.com",
    "https://graph.microsoft.com",
)


class _Server(BaseHTTPRequestHandler):
    script: dict = {}
    seen: list = []

    def _handle(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length) if length else b""
        key = (self.command, self.path.split("?")[0])
        type(self).seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
        status, payload = type(self).script.get(key, (500, {"error": "unscripted"}))
        if isinstance(status, list):
            status, payload = status.pop(0), payload
        raw = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, *args):
        pass


def _redirect(url: str, base: str) -> str:
    for host in HOSTS:
        if url.startswith(host):
            return base + url[len(host):]
    raise AssertionError(f"unexpected provider host: {url}")


class ProviderWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Server)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Server.script = {}
        _Server.seen = []

    def _post(self, url, data=None, headers=None):
        import httpx

        with httpx.Client(timeout=20) as client:
            return client.post(_redirect(url, self.base), data=data, headers=headers)

    def _request(self, method, url, json=None, headers=None):
        import httpx

        with httpx.Client(timeout=20) as client:
            return client.request(method, _redirect(url, self.base), json=json, headers=headers)

    def _flags(self):
        from services.notify.flags import NotifyFlags

        return NotifyFlags(
            push_enabled=False, calendar_feed_enabled=False, google_enabled=True, microsoft_enabled=True,
            vapid_public_key="", vapid_private_key="", vapid_subject="",
            google_client_id="google-client", google_client_secret="google-secret",
            microsoft_client_id="ms-client", microsoft_client_secret="ms-secret",
            oauth_redirect_base="https://api.example.test", token_key=Fernet.generate_key().decode("ascii"),
            public_web_url="https://league.example",
            worker_enabled=True,
        )

    @staticmethod
    def _match(**overrides):
        from tests.test_notify import _match

        return _match(**overrides)

    @staticmethod
    def _form_of(request) -> dict:
        return {key: values[0] for key, values in parse_qs(request["body"].decode("utf-8")).items()}

    def test_the_google_token_request_proves_the_pkce_verifier_matches_the_challenge(self):
        from services.notify.providers import exchange_google_code, google_authorize_url
        from services.notify.security import pkce_challenge, pkce_verifier

        state = "signed-state-value"
        secret = "jwt-secret-for-test"
        verifier = pkce_verifier(secret, state)
        flags = self._flags()
        authorize = parse_qs(urlparse(google_authorize_url(flags, state, pkce_challenge(verifier))).query)
        _Server.script[("POST", "/token")] = (200, {"access_token": "a", "refresh_token": "r-google"})

        token = exchange_google_code(flags, "auth-code-123", self._post, verifier)

        self.assertEqual(token["refresh_token"], "r-google")
        sent = self._form_of(_Server.seen[-1])
        self.assertEqual(sent["grant_type"], "authorization_code")
        self.assertEqual(sent["code"], "auth-code-123")
        self.assertEqual(sent["client_id"], "google-client")
        self.assertEqual(sent["client_secret"], "google-secret")
        self.assertEqual(sent["redirect_uri"], "https://api.example.test/calendar/google/callback")
        # The server re-derives the challenge from the verifier it was sent and compares it with
        # the challenge that went out on the authorize URL. A stolen code alone cannot pass this.
        self.assertEqual(pkce_challenge(sent["code_verifier"]), authorize["code_challenge"][0])
        self.assertEqual(authorize["code_challenge_method"], ["S256"])
        self.assertEqual(authorize["state"], [state])

    def test_a_different_state_cannot_redeem_the_code(self):
        from services.notify.providers import exchange_google_code
        from services.notify.security import pkce_challenge, pkce_verifier

        challenge = pkce_challenge(pkce_verifier("jwt-secret-for-test", "state-a"))
        wrong = pkce_verifier("jwt-secret-for-test", "state-b")
        self.assertNotEqual(pkce_challenge(wrong), challenge)
        _Server.script[("POST", "/token")] = (400, {"error": "invalid_grant"})
        from services.notify.providers import ProviderError

        with self.assertRaises(ProviderError):
            exchange_google_code(self._flags(), "auth-code-123", self._post, wrong)

    def test_microsoft_token_request_carries_the_verifier_and_the_microsoft_client(self):
        from services.notify.providers import exchange_microsoft_code

        _Server.script[("POST", "/common/oauth2/v2.0/token")] = (200, {"refresh_token": "r-ms"})
        token = exchange_microsoft_code(self._flags(), "ms-code", self._post, "the-verifier")
        self.assertEqual(token["refresh_token"], "r-ms")
        sent = self._form_of(_Server.seen[-1])
        self.assertEqual(sent["client_id"], "ms-client")
        self.assertEqual(sent["code_verifier"], "the-verifier")
        self.assertEqual(sent["redirect_uri"], "https://api.example.test/calendar/microsoft/callback")

    def test_a_refresh_sends_only_the_refresh_grant(self):
        from services.notify.providers import refresh_access_token

        _Server.script[("POST", "/token")] = (200, {"access_token": "fresh-access"})
        self.assertEqual(refresh_access_token("google", self._flags(), "stored-refresh", self._post), "fresh-access")
        sent = self._form_of(_Server.seen[-1])
        self.assertEqual(sent["grant_type"], "refresh_token")
        self.assertEqual(sent["refresh_token"], "stored-refresh")
        self.assertNotIn("code_verifier", sent)

    def test_an_expired_or_revoked_refresh_token_needs_the_admin_to_connect_again(self):
        from services.notify.providers import ProviderError, refresh_access_token

        _Server.script[("POST", "/token")] = (400, {"error": "invalid_grant"})
        with self.assertRaises(ProviderError) as raised:
            refresh_access_token("google", self._flags(), "dead-refresh", self._post)
        self.assertEqual(raised.exception.category, "reauthorization_required")
        self.assertFalse(raised.exception.retryable)

    def test_google_creates_then_updates_the_same_event_id(self):
        from services.notify.providers import google_event_id, upsert_google_event

        event_id = google_event_id(12)
        self.assertEqual(event_id, "cplmatch0000000012")
        events = f"/calendar/v3/calendars/primary/events/{event_id}"
        # First write: the event does not exist yet, so PUT answers 404 and the event is created.
        _Server.script[("PUT", events)] = (404, {"error": {"code": 404}})
        _Server.script[("POST", "/calendar/v3/calendars/primary/events")] = (200, {"id": event_id})

        result = upsert_google_event("access-token", self._match(), "https://league.example", self._request)

        self.assertEqual(result, event_id)
        self.assertEqual([(r["method"], r["path"]) for r in _Server.seen],
                         [("PUT", events), ("POST", "/calendar/v3/calendars/primary/events")])
        created = json.loads(_Server.seen[-1]["body"])
        self.assertEqual(created["id"], event_id)
        self.assertEqual(_Server.seen[-1]["headers"]["Authorization"], "Bearer access-token")

    def test_google_update_of_an_existing_event_is_a_put_to_its_id(self):
        from services.notify.providers import google_event_id, upsert_google_event

        event_id = google_event_id(12)
        events = f"/calendar/v3/calendars/primary/events/{event_id}"
        _Server.script[("PUT", events)] = (200, {"id": event_id})
        self.assertEqual(upsert_google_event("access-token", self._match(), "https://league.example", self._request), event_id)
        self.assertEqual(len(_Server.seen), 1)

    def test_a_create_that_finds_the_event_already_there_updates_it(self):
        from services.notify.providers import google_event_id, upsert_google_event

        event_id = google_event_id(12)
        events = f"/calendar/v3/calendars/primary/events/{event_id}"
        _Server.script[("PUT", events)] = ([404, 200], {"id": event_id})
        _Server.script[("POST", "/calendar/v3/calendars/primary/events")] = (409, {"error": {"code": 409}})
        self.assertEqual(upsert_google_event("access-token", self._match(), "https://league.example", self._request), event_id)
        self.assertEqual([r["method"] for r in _Server.seen], ["PUT", "POST", "PUT"])

    def test_google_rate_limit_is_retryable_with_the_providers_delay(self):
        from services.notify.providers import ProviderError, google_event_id, upsert_google_event

        events = f"/calendar/v3/calendars/primary/events/{google_event_id(12)}"
        _Server.script[("PUT", events)] = (429, {"error": {"code": 429}})
        with self.assertRaises(ProviderError) as raised:
            upsert_google_event("access-token", self._match(), "https://league.example", self._request)
        self.assertEqual(raised.exception.category, "rate_limited")
        self.assertTrue(raised.exception.retryable)

    def test_a_cancelled_match_is_written_as_cancelled_not_deleted(self):
        from services.notify.providers import google_event_id, upsert_google_event

        events = f"/calendar/v3/calendars/primary/events/{google_event_id(12)}"
        _Server.script[("PUT", events)] = (200, {"id": google_event_id(12)})
        upsert_google_event("access-token", self._match(void=True), "https://league.example", self._request)
        self.assertEqual(json.loads(_Server.seen[-1]["body"])["status"], "cancelled")

    def test_microsoft_patches_the_mapped_event_and_creates_only_when_it_is_gone(self):
        from services.notify.providers import upsert_microsoft_event

        _Server.script[("PATCH", "/v1.0/me/events/AAMkAD-1")] = (200, {"id": "AAMkAD-1"})
        self.assertEqual(upsert_microsoft_event("ms-token", self._match(), "https://league.example", "AAMkAD-1", self._request), "AAMkAD-1")
        self.assertEqual([r["method"] for r in _Server.seen], ["PATCH"])

        _Server.seen = []
        _Server.script[("PATCH", "/v1.0/me/events/AAMkAD-1")] = (404, {"error": {"code": "ErrorItemNotFound"}})
        _Server.script[("POST", "/v1.0/me/events")] = (201, {"id": "AAMkAD-2"})
        self.assertEqual(upsert_microsoft_event("ms-token", self._match(), "https://league.example", "AAMkAD-1", self._request), "AAMkAD-2")
        created = json.loads(_Server.seen[-1]["body"])
        self.assertTrue(created.get("transactionId"))

    def test_microsoft_throttling_retries_and_never_creates_a_second_event(self):
        from services.notify.providers import ProviderError, upsert_microsoft_event

        _Server.script[("PATCH", "/v1.0/me/events/AAMkAD-1")] = (429, {"error": {"code": "TooManyRequests"}})
        with self.assertRaises(ProviderError) as raised:
            upsert_microsoft_event("ms-token", self._match(), "https://league.example", "AAMkAD-1", self._request)
        self.assertEqual(raised.exception.category, "rate_limited")
        self.assertEqual([r["method"] for r in _Server.seen], ["PATCH"])

    def test_a_voided_microsoft_event_is_deleted_and_a_missing_one_is_fine(self):
        from services.notify.providers import upsert_microsoft_event

        # The stored id is kept after a delete, so a repeat void sends DELETE again and a 404 is fine.
        _Server.script[("DELETE", "/v1.0/me/events/AAMkAD-1")] = (204, None)
        self.assertEqual(upsert_microsoft_event("ms-token", self._match(void=True), "https://league.example", "AAMkAD-1", self._request), "AAMkAD-1")
        self.assertEqual(_Server.seen[-1]["method"], "DELETE")

        _Server.seen = []
        _Server.script[("DELETE", "/v1.0/me/events/AAMkAD-1")] = (404, {"error": {"code": "ErrorItemNotFound"}})
        self.assertEqual(upsert_microsoft_event("ms-token", self._match(void=True), "https://league.example", "AAMkAD-1", self._request), "AAMkAD-1")

        _Server.seen = []
        self.assertEqual(upsert_microsoft_event("ms-token", self._match(void=True), "https://league.example", None, self._request), "")
        self.assertEqual(_Server.seen, [])


if __name__ == "__main__":
    unittest.main()
