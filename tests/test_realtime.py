import asyncio
import json
import os
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import core.realtime as realtime
import routes.realtime_routes as realtime_routes
from core.realtime import (
    PUBLIC_CHANNEL,
    HubFull,
    MemoryHub,
    PublicChangeMiddleware,
    Ticket,
    TicketStore,
    admin_channel,
    format_sse,
    league_invalidated,
    login_request_changed,
    origin_allowed,
    public_area,
    publish_public_change,
    realtime_enabled,
)
from core.security import create_access_token
from routes.auth_routes import get_current_admin, get_service
from routes.realtime_routes import HEARTBEAT, REVOKE_EVERY_SECONDS, revocation_due
from schemas.auth_schema import AdminResponse


class _FlagOn:
    def __init__(self, value="true"):
        self.value = value

    def __enter__(self):
        self.previous = os.environ.get("CPL_REALTIME_ENABLED")
        if self.value is None:
            os.environ.pop("CPL_REALTIME_ENABLED", None)
        else:
            os.environ["CPL_REALTIME_ENABLED"] = self.value

    def __exit__(self, *_):
        if self.previous is None:
            os.environ.pop("CPL_REALTIME_ENABLED", None)
        else:
            os.environ["CPL_REALTIME_ENABLED"] = self.previous


class RealtimeEventTests(unittest.TestCase):
    def test_league_notice_names_only_the_area(self):
        body = json.loads(league_invalidated("matches").to_json())
        self.assertEqual(body["eventType"], "league.invalidated")
        self.assertEqual(body["eventVersion"], 1)
        self.assertEqual(body["entityType"], "area")
        self.assertEqual(body["entityId"], "matches")
        self.assertEqual(body["payload"], {"area": "matches"})
        frame = format_sse(league_invalidated("matches").to_json())
        self.assertIn("event: league.invalidated\n", frame)
        self.assertTrue(frame.endswith("\n\n"))

    def test_login_notice_carries_no_request_details(self):
        body = json.loads(login_request_changed(7).to_json())
        self.assertEqual(body["eventType"], "admin.login_request.changed")
        self.assertEqual(body["entityId"], "7")
        self.assertEqual(body["payload"], {})

    def test_only_public_writes_map_to_an_area(self):
        self.assertEqual(public_area("POST", "/matches"), "matches")
        self.assertEqual(public_area("PUT", "/matches/4/stats"), "matches")
        self.assertEqual(public_area("DELETE", "/rosters/remove"), "rosters")
        self.assertEqual(public_area("PUT", "/settings"), "settings")
        self.assertIsNone(public_area("GET", "/matches"))
        self.assertIsNone(public_area("POST", "/auth/login"))
        self.assertIsNone(public_area("PATCH", "/admins/3"))
        self.assertIsNone(public_area("POST", "/assistant/admin/chat"))

    def test_origin_must_be_on_the_allowed_list(self):
        allowed = ["http://localhost:5179", "https://aroyofficial.github.io"]
        self.assertTrue(origin_allowed(None, allowed))
        self.assertTrue(origin_allowed("https://aroyofficial.github.io/", allowed))
        self.assertFalse(origin_allowed("https://evil.example", allowed))

    def test_the_flag_defaults_off(self):
        with _FlagOn(None):
            self.assertFalse(realtime_enabled())
        with _FlagOn("true"):
            self.assertTrue(realtime_enabled())

    def test_publish_does_nothing_while_the_flag_is_off(self):
        with _FlagOn(None):
            before = realtime.hub.published
            publish_public_change("matches")
            self.assertEqual(realtime.hub.published, before)


class HeartbeatTests(unittest.TestCase):
    def test_a_heartbeat_is_an_event_the_browser_can_see(self):
        self.assertIn("event: realtime.heartbeat\n", HEARTBEAT)
        self.assertIn("data: {}", HEARTBEAT)
        self.assertTrue(HEARTBEAT.endswith("\n\n"))
        self.assertNotIn(": keepalive", HEARTBEAT)

    def test_the_account_is_rechecked_once_a_minute(self):
        self.assertTrue(revocation_due(None, 10))
        self.assertFalse(revocation_due(10, 10 + REVOKE_EVERY_SECONDS - 1))
        self.assertTrue(revocation_due(10, 10 + REVOKE_EVERY_SECONDS))


class MemoryHubTests(unittest.TestCase):
    def test_only_the_matching_channel_receives_the_event(self):
        async def scenario():
            live = MemoryHub()
            own = await live.subscribe(admin_channel(3))
            other = await live.subscribe(admin_channel(9))
            live.publish(admin_channel(3), login_request_changed(3))
            await asyncio.sleep(0)
            self.assertFalse(own.empty())
            self.assertTrue(other.empty())

        asyncio.run(scenario())

    def test_a_full_hub_refuses_another_subscriber(self):
        async def scenario():
            live = MemoryHub(max_subscribers=1)
            await live.subscribe(PUBLIC_CHANNEL)
            with self.assertRaises(HubFull):
                await live.subscribe(PUBLIC_CHANNEL)
            self.assertEqual(live.rejected, 1)

        asyncio.run(scenario())

    def test_a_slow_subscriber_drops_the_oldest_event(self):
        async def scenario():
            live = MemoryHub()
            queue = await live.subscribe(PUBLIC_CHANNEL)
            for _ in range(40):
                live.publish(PUBLIC_CHANNEL, league_invalidated("matches"))
            await asyncio.sleep(0)
            self.assertEqual(queue.qsize(), 32)
            self.assertGreaterEqual(live.dropped, 1)
            live.unsubscribe(PUBLIC_CHANNEL, queue)
            self.assertEqual(live.subscriber_count(), 0)

        asyncio.run(scenario())

    def test_publish_from_a_worker_thread_reaches_the_loop(self):
        async def scenario():
            live = MemoryHub()
            queue = await live.subscribe(PUBLIC_CHANNEL)
            worker = threading.Thread(target=live.publish, args=(PUBLIC_CHANNEL, league_invalidated("teams")))
            worker.start()
            worker.join()
            body = await asyncio.wait_for(queue.get(), timeout=2)
            self.assertEqual(json.loads(body)["entityId"], "teams")

        asyncio.run(scenario())


class TicketStoreTests(unittest.TestCase):
    def setUp(self):
        self.ticket = Ticket(admin_id=5, username="a.b", token_version=2, token_expires_at=0)

    def test_a_ticket_works_once(self):
        store = TicketStore()
        value = store.issue(self.ticket, now=0)
        self.assertEqual(store.consume(value, now=1), self.ticket)
        self.assertIsNone(store.consume(value, now=1))

    def test_an_old_ticket_is_refused(self):
        store = TicketStore(ttl_seconds=60)
        value = store.issue(self.ticket, now=0)
        self.assertIsNone(store.consume(value, now=61))

    def test_unknown_or_blank_tickets_are_refused(self):
        store = TicketStore()
        self.assertIsNone(store.consume("nope"))
        self.assertIsNone(store.consume(""))

    def test_the_store_has_a_ceiling(self):
        store = TicketStore(limit=1)
        store.issue(self.ticket, now=0)
        with self.assertRaises(HubFull):
            store.issue(self.ticket, now=0)


def _admin():
    return AdminResponse(id=5, username="a.b", is_active=True, must_change_password=False, permissions=[])


def _route_app():
    app = FastAPI()
    app.add_middleware(PublicChangeMiddleware)
    app.include_router(realtime_routes.router)
    app.dependency_overrides[get_current_admin] = _admin

    @app.post("/matches")
    def write_match():
        return {"ok": True}

    @app.post("/players")
    def refuse_player():
        raise HTTPException(status_code=400, detail="no")

    @app.post("/auth/login")
    def login():
        return {"ok": True}

    return app


class RealtimeRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_route_app())

    def test_streams_are_hidden_while_the_flag_is_off(self):
        with _FlagOn(None):
            self.assertEqual(self.client.get("/realtime/public").status_code, 404)
            self.assertEqual(self.client.get("/realtime/admin?ticket=x").status_code, 404)
            self.assertFalse(self.client.get("/realtime/health").json()["enabled"])

    def test_a_foreign_origin_cannot_subscribe(self):
        with _FlagOn():
            response = self.client.get("/realtime/public", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_the_admin_stream_needs_a_ticket(self):
        with _FlagOn():
            self.assertEqual(self.client.get("/realtime/admin").status_code, 401)
            self.assertEqual(self.client.get("/realtime/admin?ticket=forged").status_code, 401)

    def test_a_ticket_needs_a_signed_in_admin(self):
        app = _route_app()
        app.dependency_overrides.clear()
        app.dependency_overrides[get_service] = lambda: None
        with _FlagOn():
            response = TestClient(app).post("/realtime/ticket")
        self.assertEqual(response.status_code, 401)

    def test_a_signed_in_admin_gets_a_ticket_bound_to_the_token(self):
        token = create_access_token(5, "a.b", 2)
        with _FlagOn():
            response = self.client.post("/realtime/ticket", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(response.status_code, 200)
        granted = realtime.tickets.consume(response.json()["ticket"])
        self.assertEqual((granted.admin_id, granted.username, granted.token_version), (5, "a.b", 2))

    def test_only_successful_public_writes_publish(self):
        calls = []
        with patch.object(realtime, "publish_public_change", calls.append):
            self.client.post("/matches")
            self.client.post("/players")
            self.client.post("/auth/login")
            self.client.get("/realtime/health")
        self.assertEqual(calls, ["matches"])


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveStreamTests(unittest.TestCase):
    """Runs uvicorn for real and reads the stream over HTTP."""

    @classmethod
    def setUpClass(cls):
        cls.flag = _FlagOn()
        cls.flag.__enter__()
        cls.port = _free_port()
        config = uvicorn.Config(_route_app(), host="127.0.0.1", port=cls.port, log_level="warning")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 10
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=10)
        cls.flag.__exit__()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _read_event(self, stream, deadline, name):
        lines = []
        while time.time() < deadline:
            line = stream.readline().decode("utf-8")
            if not line:
                break
            if line == "\n":
                if f"event: {name}" in lines:
                    return lines
                lines = []
                continue
            lines.append(line.rstrip("\n"))
        self.fail(f"no {name} event arrived")

    def _wait_for_subscribers(self, count):
        deadline = time.time() + 5
        while realtime.hub.subscriber_count() < count and time.time() < deadline:
            time.sleep(0.05)

    def test_a_write_reaches_an_open_public_stream(self):
        with urllib.request.urlopen(self._url("/realtime/public"), timeout=10) as stream:
            self.assertEqual(stream.headers["Content-Type"].split(";")[0], "text/event-stream")
            self._wait_for_subscribers(1)
            urllib.request.urlopen(urllib.request.Request(self._url("/matches"), method="POST"), timeout=5).read()
            lines = self._read_event(stream, time.time() + 5, "league.invalidated")
        self.assertIn("event: league.invalidated", lines)
        data = json.loads(next(item for item in lines if item.startswith("data: "))[6:])
        self.assertEqual(data["payload"], {"area": "matches"})

    def test_a_private_notice_reaches_only_its_admin(self):
        ticket = realtime.tickets.issue(Ticket(5, "a.b", 2, int(time.time()) + 600))
        with urllib.request.urlopen(self._url(f"/realtime/admin?ticket={ticket}"), timeout=10) as stream:
            self._wait_for_subscribers(1)
            realtime.publish_login_request_changed(9)
            realtime.publish_login_request_changed(5)
            lines = self._read_event(stream, time.time() + 5, "admin.login_request.changed")
        data = json.loads(next(item for item in lines if item.startswith("data: "))[6:])
        self.assertEqual(data["entityId"], "5")

    def test_a_revoked_admin_stream_is_closed(self):
        ticket = realtime.tickets.issue(Ticket(5, "a.b", 2, int(time.time()) + 600))
        with patch.object(realtime_routes, "KEEPALIVE_SECONDS", 0.2), \
                patch.object(realtime_routes, "_admin_still_valid", lambda _ticket: False):
            with urllib.request.urlopen(self._url(f"/realtime/admin?ticket={ticket}"), timeout=10) as stream:
                lines = self._read_event(stream, time.time() + 5, "realtime.revoked")
                self.assertEqual(stream.read(), b"")
        self.assertIn("event: realtime.revoked", lines)

    def test_a_quiet_stream_sends_a_heartbeat(self):
        with patch.object(realtime_routes, "KEEPALIVE_SECONDS", 0.2):
            with urllib.request.urlopen(self._url("/realtime/public"), timeout=10) as stream:
                lines = self._read_event(stream, time.time() + 5, "realtime.heartbeat")
        self.assertIn("data: {}", lines)

    def test_an_admin_stream_rechecks_the_account_once_a_minute(self):
        calls = []
        ticket = realtime.tickets.issue(Ticket(5, "a.b", 2, int(time.time()) + 600))

        def still_valid(_ticket):
            calls.append(1)
            return True

        with patch.object(realtime_routes, "KEEPALIVE_SECONDS", 0.2), \
                patch.object(realtime_routes, "_admin_still_valid", still_valid):
            with urllib.request.urlopen(self._url(f"/realtime/admin?ticket={ticket}"), timeout=10) as stream:
                for _ in range(3):
                    self._read_event(stream, time.time() + 5, "realtime.heartbeat")
        self.assertEqual(calls, [1])

    def test_a_used_ticket_cannot_open_a_second_stream(self):
        ticket = realtime.tickets.issue(Ticket(5, "a.b", 2, int(time.time()) + 600))
        with urllib.request.urlopen(self._url(f"/realtime/admin?ticket={ticket}"), timeout=10):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(self._url(f"/realtime/admin?ticket={ticket}"), timeout=5)
        self.assertEqual(caught.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
