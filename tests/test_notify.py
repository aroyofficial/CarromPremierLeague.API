"""Match alerts and calendar feeds."""

from __future__ import annotations

import os
import unittest
from datetime import date, datetime, timezone

from tests import test_auth_service as _env  # noqa: F401  (sets required app env before config imports)

from cryptography.fernet import Fernet

os.environ.setdefault("CPL_PUSH_ENABLED", "false")
os.environ.setdefault("CPL_CALENDAR_FEED_ENABLED", "false")
os.environ.setdefault("CPL_GOOGLE_CALENDAR_ENABLED", "false")
os.environ.setdefault("CPL_MICROSOFT_CALENDAR_ENABLED", "false")

from services.notify.copy import alert_body, summary
from services.notify.delivery import delivery_plan, subscription_wants
from services.notify.events import classify, idempotency_key
from services.notify.flags import NotifyFlags, load_flags
from services.notify.ics import calendar_etag, calendar_text, event_uid, not_modified, sequence_for
from services.notify.limits import RateLimiter
from services.notify.providers import (
    ProviderNotConfigured,
    content_hash,
    google_authorize_url,
    google_event_body,
    microsoft_event_body,
)
from services.notify.push import push_payload
from services.notify.security import StateError, decrypt_secret, encrypt_secret, read_state, sign_state
from services.notify.worker import deliver_item, enqueue_reminders
import services.notify.worker as worker_module


def _flags(**overrides) -> NotifyFlags:
    base = dict(
        push_enabled=False,
        calendar_feed_enabled=False,
        google_enabled=False,
        microsoft_enabled=False,
        vapid_public_key="",
        vapid_private_key="",
        vapid_subject="",
        google_client_id="",
        google_client_secret="",
        microsoft_client_id="",
        microsoft_client_secret="",
        oauth_redirect_base="",
        token_key="",
        public_web_url="https://league.example",
        worker_enabled=True,
        push_live_enabled=True,
        calendar_live_updates_enabled=True,
    )
    base.update(overrides)
    return NotifyFlags(**base)


def _match(**overrides) -> dict:
    match = {
        "id": 12,
        "season_id": 3,
        "season_name": "Season 3",
        "team1_id": 1,
        "team2_id": 2,
        "team1_name": "Red",
        "team2_name": "Blue",
        "scheduled_date": date(2026, 11, 18),
        "status": 1,
        "void": False,
        "category": 1,
        "outcome": 3,
        "net_points": None,
        "updated_at": datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc),
    }
    match.update(overrides)
    return match


class FlagTests(unittest.TestCase):
    def test_every_channel_is_off_until_its_own_switch_and_secret_exist(self):
        flags = load_flags({})
        self.assertFalse(flags.push_ready())
        self.assertFalse(flags.feed_ready())
        self.assertFalse(flags.google_ready())
        self.assertFalse(flags.microsoft_ready())
        self.assertIn("CPL_CALENDAR_FEED_ENABLED", flags.feed_block_reason())

    def test_a_push_switch_without_keys_stays_blocked(self):
        flags = _flags(push_enabled=True)
        self.assertTrue(flags.push_enabled)
        self.assertFalse(flags.push_ready())
        self.assertIn("VAPID", flags.push_block_reason())

    def test_the_feed_does_not_require_a_push_key(self):
        flags = _flags(calendar_feed_enabled=True, push_enabled=False)
        self.assertTrue(flags.feed_ready())
        self.assertFalse(flags.push_ready())

    def test_google_stays_blocked_when_the_secret_is_missing(self):
        flags = _flags(google_enabled=True, google_client_id="id")
        self.assertFalse(flags.google_ready())
        with self.assertRaises(ProviderNotConfigured):
            google_authorize_url(flags, "state")


class LifecycleTests(unittest.TestCase):
    def test_create_schedule_live_finish_score_cancel_and_move(self):
        created = _match()
        self.assertEqual(classify("created", None, created), ["scheduled"])
        moved = _match(scheduled_date=date(2026, 11, 20))
        self.assertEqual(classify("updated", created, moved), ["rescheduled"])
        live = _match(status=2)
        self.assertEqual(classify("updated", created, live), ["live"])
        finished = _match(status=3, outcome=1, net_points=4)
        self.assertEqual(classify("updated", live, finished), ["ended"])
        rescored = _match(status=3, outcome=2, net_points=2)
        self.assertEqual(classify("updated", finished, rescored), ["score"])
        self.assertEqual(classify("deleted", created, None), ["cancelled"])
        self.assertEqual(classify("reset", finished, created), ["reset"])

    def test_a_date_change_and_a_live_start_in_one_save_both_notify(self):
        before = _match()
        after = _match(status=2, scheduled_date=date(2026, 11, 19), team1_id=9, team1_name="Gold")
        self.assertEqual(classify("updated", before, after), ["rescheduled", "teams", "live"])

    def test_the_same_save_uses_the_same_idempotency_key(self):
        stamp = datetime(2026, 11, 18, 1, 2, 3, 4, tzinfo=timezone.utc)
        self.assertEqual(
            idempotency_key(12, "live", stamp),
            idempotency_key(12, "live", stamp),
        )
        self.assertNotEqual(idempotency_key(12, "live", stamp), idempotency_key(12, "ended", stamp))


class CalendarTextTests(unittest.TestCase):
    def test_a_feed_keeps_a_stable_uid_and_replaces_the_event_when_it_changes(self):
        first = calendar_text([_match()], "Season 3", "https://league.example", now=datetime(2026, 11, 1, tzinfo=timezone.utc))
        later = calendar_text(
            [_match(updated_at=datetime(2026, 11, 2, tzinfo=timezone.utc), status=2)],
            "Season 3",
            "https://league.example",
            now=datetime(2026, 11, 2, tzinfo=timezone.utc),
        )
        self.assertIn("UID:match-12@carrompremierleague", first)
        self.assertEqual(event_uid(12), "match-12@carrompremierleague")
        self.assertLess(sequence_for(_match()), sequence_for(_match(updated_at=datetime(2026, 11, 2, tzinfo=timezone.utc))))
        self.assertIn("STATUS:CONFIRMED", first)
        self.assertIn("SUMMARY:Live: Red vs Blue", later)
        self.assertIn("BEGIN:VALARM", first)
        self.assertNotEqual(calendar_etag(first), calendar_etag(later))

    def test_a_cancelled_match_is_cancelled_in_the_calendar(self):
        body = calendar_text([_match(void=True)], "Season 3", "https://league.example")
        self.assertIn("STATUS:CANCELLED", body)
        self.assertIn("SUMMARY:Cancelled: Red vs Blue", body)
        self.assertNotIn("BEGIN:VALARM", body)

    def test_long_lines_are_folded_and_commas_are_escaped(self):
        match = _match(team1_name="Red, Gold " + ("Strikers" * 20))
        body = calendar_text([match], "Season 3", "https://league.example")
        self.assertIn("Red\\, Gold", body)
        for line in body.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)

    def test_an_etag_match_is_not_modified(self):
        self.assertTrue(not_modified('"abc"', "abc"))
        self.assertFalse(not_modified('"other"', "abc"))
        self.assertFalse(not_modified(None, "abc"))


class DeliveryTests(unittest.TestCase):
    def test_a_device_can_limit_itself_to_one_team_and_one_kind_of_alert(self):
        match = _match()
        quiet = {
            "season_id": 3,
            "team_id": 1,
            "notify_live": False,
            "notify_scheduled": True,
        }
        self.assertTrue(subscription_wants(quiet, "scheduled", match))
        self.assertFalse(subscription_wants(quiet, "live", match))
        self.assertFalse(subscription_wants(quiet, "scheduled", _match(team1_id=8, team2_id=9)))
        self.assertFalse(subscription_wants({"season_id": 4}, "scheduled", match))

    def test_push_can_be_planned_while_google_stays_off(self):
        flags = _flags(
            push_enabled=True,
            vapid_public_key="pub",
            vapid_private_key="priv",
            vapid_subject="mailto:cpl@example.com",
        )
        plan = delivery_plan(
            "live",
            _match(status=2),
            [{"id": 7, "void": False, "notify_live": True, "season_id": None, "team_id": None}],
            [{"id": 4, "provider": "google", "void": False}],
            flags,
        )
        self.assertEqual([item["channel"] for item in plan], ["push"])

    def test_alert_text_names_the_change(self):
        self.assertIn("moved from", alert_body("rescheduled", _match(scheduled_date=date(2026, 11, 20)), _match()))
        self.assertIn("won by 4", summary(_match(status=3, outcome=1, net_points=4)))
        payload = push_payload("live", _match(status=2), None, "https://league.example")
        self.assertNotIn("priv", str(payload))
        self.assertEqual(payload["url"], "https://league.example/schedule#match-12")

    def test_google_and_microsoft_bodies_cancel_and_hash_the_same_event(self):
        cancelled = _match(void=True)
        google = google_event_body(cancelled, "https://league.example")
        microsoft = microsoft_event_body(cancelled, "https://league.example")
        self.assertEqual(google["status"], "cancelled")
        self.assertNotIn("isCancelled", microsoft)
        self.assertEqual(google["start"]["date"], "2026-11-18")
        self.assertEqual(content_hash(google), content_hash(google))
        self.assertIn("does not record a start time or a venue", google["description"])


class SecurityTests(unittest.TestCase):
    def test_state_expires_and_rejects_a_tampered_token(self):
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        token = sign_state("secret", "google", 5, now, ttl_seconds=60)
        self.assertEqual(read_state("secret", token, now, "google"), 5)
        with self.assertRaises(StateError):
            read_state("secret", token, datetime(2026, 11, 18, 0, 2, tzinfo=timezone.utc), "google")
        with self.assertRaises(StateError):
            read_state("secret", token + "x", now, "google")
        with self.assertRaises(StateError):
            read_state("secret", token, now, "microsoft")

    def test_refresh_tokens_are_not_stored_in_the_clear(self):
        key = Fernet.generate_key().decode("ascii")
        stored = encrypt_secret(key, "refresh-token")
        self.assertNotIn("refresh-token", stored)
        self.assertEqual(decrypt_secret(key, stored), "refresh-token")
        with self.assertRaises(StateError):
            encrypt_secret("not-a-key", "refresh-token")

    def test_public_subscribe_is_rate_limited(self):
        limiter = RateLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.allow("1.1.1.1", now=100))
        self.assertTrue(limiter.allow("1.1.1.1", now=101))
        self.assertFalse(limiter.allow("1.1.1.1", now=102))
        self.assertTrue(limiter.allow("1.1.1.1", now=161))


class WorkerTests(unittest.TestCase):
    def test_an_unchanged_google_event_is_not_sent_again(self):
        flags = _flags(
            google_enabled=True,
            google_client_id="id",
            google_client_secret="secret",
            oauth_redirect_base="https://api.example",
            token_key=Fernet.generate_key().decode("ascii"),
        )
        match = _match()
        body = google_event_body(match, flags.public_web_url)
        calls = []

        class Repo:
            def match_notice(self, match_id):
                return match

            def active_subscriptions(self):
                return []

            def active_connections(self):
                return [{
                    "id": 1,
                    "provider": "google",
                    "void": False,
                    "refresh_token": encrypt_secret(flags.token_key, "refresh"),
                }]

            def event_map(self, connection_id, match_id):
                return {"external_event_id": "cplmatch0000000012", "content_hash": content_hash(body)}

            def record_delivery(self, outbox_id, channel, target, status, detail):
                calls.append((channel, status, detail))

            def mark_done(self, item_id):
                calls.append(("done", item_id))

            def mark_retry(self, item_id, attempts, reason):
                raise AssertionError(reason)

        item = {
            "Id": 9,
            "MatchId": 12,
            "EventType": "scheduled",
            "Attempts": 1,
            "Payload": '{"event":"scheduled","match":{"id":12},"before":null}',
        }
        deliver_item(Repo(), item, flags, post=lambda *args, **kwargs: calls.append("post"), request=lambda *args, **kwargs: calls.append("request"))
        self.assertIn(("google", "skipped", "unchanged"), calls)
        self.assertNotIn("post", calls)
        self.assertNotIn("request", calls)

    def test_a_reminder_is_queued_once_per_match_day(self):
        worker_module._last_reminder_day = None
        queued = []

        class Repo:
            def due_reminders(self, day):
                return [_match()]

            def enqueue(self, match, event, key=None):
                queued.append((event, key))

        day = date(2026, 11, 18)
        enqueue_reminders(Repo(), day)
        enqueue_reminders(Repo(), day)
        self.assertEqual(queued, [("reminder", "12:reminder:2026-11-18")])


class ConnectRouteTests(unittest.TestCase):
    """The account connect routes must refuse anonymous callers before any provider code runs."""

    def test_connect_needs_a_signed_in_admin(self):
        from fastapi.testclient import TestClient
        from main import app
        from routes.auth_routes import get_service

        # The anonymous path returns 401 before the service is used, so no database is needed.
        app.dependency_overrides[get_service] = lambda: None
        try:
            client = TestClient(app)
            for path in ("/calendar/google/connect", "/calendar/microsoft/connect"):
                with self.subTest(path=path):
                    response = client.post(path)
                    self.assertEqual(response.status_code, 401)
        finally:
            app.dependency_overrides.pop(get_service, None)


class _Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class RetryTests(unittest.TestCase):
    def test_each_attempt_waits_longer_and_the_wait_is_capped(self):
        from services.notify.retry import retry_delay_seconds

        delays = [retry_delay_seconds(n, rand=lambda: 0.5) for n in range(1, 10)]
        self.assertEqual(delays[:3], [60, 120, 240])
        self.assertEqual(delays[-1], 3600)
        self.assertEqual(delays, sorted(delays))

    def test_jitter_stays_within_twenty_percent(self):
        from services.notify.retry import retry_delay_seconds

        self.assertEqual(retry_delay_seconds(2, rand=lambda: 0.0), 96)
        self.assertEqual(retry_delay_seconds(2, rand=lambda: 0.999), 143)

    def test_a_providers_retry_after_is_honoured_when_longer_and_bounded(self):
        from services.notify.retry import retry_delay_seconds

        self.assertEqual(retry_delay_seconds(1, retry_after=600, rand=lambda: 0.5), 600)
        self.assertEqual(retry_delay_seconds(1, retry_after=10, rand=lambda: 0.5), 60)
        self.assertEqual(retry_delay_seconds(1, retry_after=10**6, rand=lambda: 0.5), 6 * 60 * 60)


class ProviderFailureTests(unittest.TestCase):
    def test_a_revoked_refresh_grant_needs_the_admin_to_reconnect(self):
        from services.notify.providers import ReauthorizationRequired, refresh_access_token

        flags = _flags(
            google_enabled=True,
            google_client_id="id",
            google_client_secret="secret",
            oauth_redirect_base="https://api.example",
            token_key=Fernet.generate_key().decode("ascii"),
        )
        with self.assertRaises(ReauthorizationRequired):
            refresh_access_token("google", flags, "refresh", lambda url, data=None, headers=None: _Response(400, {"error": "invalid_grant"}))

    def test_rate_limits_and_outages_retry_and_rejections_do_not(self):
        from services.notify.providers import ProviderRateLimited, ProviderRejected, ProviderUnavailable, upsert_google_event

        match = _match()

        def failing(status, response_headers=None):
            return lambda method, url, json=None, headers=None: _Response(status, {"error": "secret-body-text"}, response_headers)

        with self.assertRaises(ProviderRateLimited) as limited:
            upsert_google_event("token", match, "https://site.example", failing(429, {"Retry-After": "120"}))
        self.assertEqual(limited.exception.retry_after, 120)
        self.assertTrue(limited.exception.retryable)
        with self.assertRaises(ProviderUnavailable):
            upsert_google_event("token", match, "https://site.example", failing(503))
        with self.assertRaises(ProviderRejected) as rejected:
            upsert_google_event("token", match, "https://site.example", failing(400))
        self.assertFalse(rejected.exception.retryable)
        self.assertNotIn("secret-body-text", str(rejected.exception))


class _ScriptedProvider:
    """Answers provider calls in order, the way Google and Graph document them, and records each call."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, str, dict | None]] = []

    def __call__(self, method, url, json=None, headers=None):
        self.calls.append((method, url, json))
        status, body = self.answers.pop(0)
        return _Response(status, body)

    @property
    def methods(self):
        return [call[0] for call in self.calls]


class ProviderWriteTests(unittest.TestCase):
    def test_google_creates_a_new_match_event_under_its_stable_id(self):
        from services.notify.providers import GOOGLE_EVENTS, google_event_id, upsert_google_event

        provider = _ScriptedProvider((404, {"error": {"code": 404}}), (200, {"id": google_event_id(12)}))
        self.assertEqual(upsert_google_event("token", _match(), "https://site.example", provider), "cplmatch0000000012")
        self.assertEqual(provider.methods, ["PUT", "POST"])
        self.assertEqual(provider.calls[1][1], GOOGLE_EVENTS)
        self.assertEqual(provider.calls[1][2]["id"], "cplmatch0000000012")

    def test_google_updates_an_existing_event_in_one_call(self):
        from services.notify.providers import upsert_google_event

        provider = _ScriptedProvider((200, {}))
        upsert_google_event("token", _match(), "https://site.example", provider)
        self.assertEqual(provider.methods, ["PUT"])

    def test_google_create_race_lands_on_the_one_existing_event(self):
        from services.notify.providers import upsert_google_event

        provider = _ScriptedProvider((404, {}), (409, {}), (200, {}))
        upsert_google_event("token", _match(), "https://site.example", provider)
        self.assertEqual(provider.methods, ["PUT", "POST", "PUT"])

    def test_google_event_ids_use_only_the_characters_google_allows(self):
        from services.notify.providers import google_event_id

        event_id = google_event_id(987654)
        self.assertTrue(5 <= len(event_id) <= 1024)
        self.assertTrue(set(event_id) <= set("0123456789abcdefghijklmnopqrstuv"))

    def test_microsoft_creates_once_and_returns_the_graph_id(self):
        from services.notify.providers import upsert_microsoft_event

        provider = _ScriptedProvider((201, {"id": "AAMkAD-1"}))
        self.assertEqual(upsert_microsoft_event("token", _match(), "https://site.example", None, provider), "AAMkAD-1")
        self.assertEqual(provider.methods, ["POST"])
        self.assertEqual(provider.calls[0][2]["transactionId"], "cplmatch0000000012")

    def test_microsoft_updates_the_mapped_event_without_creating_another(self):
        from services.notify.providers import upsert_microsoft_event

        provider = _ScriptedProvider((200, {"id": "AAMkAD-1"}))
        self.assertEqual(upsert_microsoft_event("token", _match(), "https://site.example", "AAMkAD-1", provider), "AAMkAD-1")
        self.assertEqual(provider.methods, ["PATCH"])

    def test_microsoft_recreates_only_when_the_user_deleted_the_event(self):
        from services.notify.providers import upsert_microsoft_event

        provider = _ScriptedProvider((404, {}), (201, {"id": "AAMkAD-2"}))
        self.assertEqual(upsert_microsoft_event("token", _match(), "https://site.example", "AAMkAD-1", provider), "AAMkAD-2")
        self.assertEqual(provider.methods, ["PATCH", "POST"])

    def test_microsoft_outage_or_rate_limit_never_creates_a_duplicate(self):
        from services.notify.providers import ProviderRateLimited, ProviderUnavailable, upsert_microsoft_event

        for status, error in ((503, ProviderUnavailable), (429, ProviderRateLimited), (401, ProviderUnavailable)):
            provider = _ScriptedProvider((status, {}))
            with self.assertRaises(error):
                upsert_microsoft_event("token", _match(), "https://site.example", "AAMkAD-1", provider)
            self.assertEqual(provider.methods, ["PATCH"])

    def test_microsoft_cancellation_deletes_the_mapped_event(self):
        from services.notify.providers import upsert_microsoft_event

        provider = _ScriptedProvider((204, {}))
        upsert_microsoft_event("token", _match(void=True), "https://site.example", "AAMkAD-1", provider)
        self.assertEqual(provider.methods, ["DELETE"])
        already_gone = _ScriptedProvider((404, {}))
        upsert_microsoft_event("token", _match(void=True), "https://site.example", "AAMkAD-1", already_gone)
        nothing_mapped = _ScriptedProvider()
        self.assertEqual(upsert_microsoft_event("token", _match(void=True), "https://site.example", None, nothing_mapped), "")
        self.assertEqual(nothing_mapped.methods, [])

    def test_microsoft_create_without_an_id_is_retried_not_mapped(self):
        from services.notify.providers import ProviderUnavailable, upsert_microsoft_event

        with self.assertRaises(ProviderUnavailable):
            upsert_microsoft_event("token", _match(), "https://site.example", None, _ScriptedProvider((201, {})))


class ProviderOutcomeWorkerTests(unittest.TestCase):
    @staticmethod
    def _flags_and_connection():
        flags = _flags(
            google_enabled=True,
            google_client_id="id",
            google_client_secret="secret",
            oauth_redirect_base="https://api.example",
            token_key=Fernet.generate_key().decode("ascii"),
        )
        connection = {
            "id": 7,
            "provider": "google",
            "void": False,
            "refresh_token": encrypt_secret(flags.token_key, "refresh"),
        }
        return flags, connection

    @staticmethod
    def _item():
        return {
            "Id": 9,
            "MatchId": 12,
            "EventType": "scheduled",
            "Attempts": 1,
            "Payload": '{"event":"scheduled","match":{"id":12},"before":null}',
        }

    def test_a_revoked_grant_stops_the_connection_and_is_not_retried(self):
        flags, connection = self._flags_and_connection()
        events = []

        class Repo:
            def match_notice(self, match_id):
                return _match()

            def active_subscriptions(self):
                return []

            def active_connections(self):
                return [connection]

            def event_map(self, connection_id, match_id):
                return None

            def record_delivery(self, outbox_id, channel, target, status, detail):
                events.append(("delivery", channel, status, detail))

            def set_connection_status(self, connection_id, status):
                events.append(("connection", connection_id, status))

            def mark_done(self, item_id):
                events.append(("done", item_id))

            def mark_retry(self, *args, **kwargs):
                raise AssertionError("a revoked grant must not be retried")

        deliver_item(
            Repo(),
            self._item(),
            flags,
            post=lambda url, data=None, headers=None: _Response(400, {"error": "invalid_grant"}),
            request=lambda *args, **kwargs: self.fail("no event write without a token"),
        )
        self.assertIn(("connection", 7, "revoked"), events)
        self.assertIn(("delivery", "google", "failed", "reauthorization_required"), events)
        self.assertIn(("done", 9), events)

    def test_a_rate_limited_event_write_is_retried_with_the_providers_wait(self):
        flags, connection = self._flags_and_connection()
        events = []

        class Repo:
            def match_notice(self, match_id):
                return _match()

            def active_subscriptions(self):
                return []

            def active_connections(self):
                return [connection]

            def event_map(self, connection_id, match_id):
                return None

            def record_delivery(self, outbox_id, channel, target, status, detail):
                events.append(("delivery", channel, status, detail))

            def set_connection_status(self, connection_id, status):
                events.append(("connection", connection_id, status))

            def mark_done(self, item_id):
                events.append(("done", item_id))

            def mark_retry(self, item_id, attempts, reason, retry_after=None):
                events.append(("retry", reason, retry_after))

        deliver_item(
            Repo(),
            self._item(),
            flags,
            post=lambda url, data=None, headers=None: _Response(200, {"access_token": "a"}),
            request=lambda method, url, json=None, headers=None: _Response(429, {}, {"Retry-After": "300"}),
        )
        self.assertIn(("retry", "rate_limited", 300), events)
        self.assertFalse([event for event in events if event[0] == "done"])
        self.assertFalse([event for event in events if event[0] == "connection"])


class DeliveryOrderingTests(unittest.TestCase):
    @staticmethod
    def _item(event="live"):
        return {
            "Id": 9,
            "MatchId": 12,
            "EventType": event,
            "Attempts": 1,
            "Payload": '{"event":"%s","match":{"id":12,"status":2},"before":null}' % event,
        }

    def test_a_push_service_that_reports_the_subscription_gone_voids_it(self):
        from unittest import mock

        flags = _flags(
            push_enabled=True,
            vapid_public_key="pub",
            vapid_private_key="priv",
            vapid_subject="mailto:cpl@example.com",
        )
        events = []

        class Repo:
            def match_notice(self, match_id):
                return _match(status=2)

            def active_subscriptions(self):
                return [{
                    "id": 5,
                    "endpoint": "https://push.example/abc",
                    "p256dh": "key",
                    "auth": "secret",
                    "void": False,
                    "notify_scheduled": True,
                    "notify_changed": True,
                    "notify_cancelled": True,
                    "notify_live": True,
                    "notify_ended": True,
                    "notify_score": True,
                    "notify_reminder": True,
                }]

            def active_connections(self):
                return []

            def record_delivery(self, outbox_id, channel, target, status, detail):
                events.append(("delivery", channel, status, detail))

            def note_push_failure(self, subscription_id, gone):
                events.append(("failure", subscription_id, gone))

            def mark_done(self, item_id):
                events.append(("done", item_id))

            def mark_retry(self, *args, **kwargs):
                raise AssertionError("a gone subscription is not retried")

        gone = {"ok": False, "blocked": False, "gone": True, "reason": "push service returned 410"}
        with mock.patch("services.notify.worker.send_web_push", return_value=gone):
            deliver_item(Repo(), self._item(), flags, post=None, request=None)
        self.assertIn(("failure", 5, True), events)
        self.assertIn(("done", 9), events)

    def test_a_stale_live_notice_cannot_put_a_finished_match_back_to_live(self):
        flags = _flags(
            google_enabled=True,
            google_client_id="id",
            google_client_secret="secret",
            oauth_redirect_base="https://api.example",
            token_key=Fernet.generate_key().decode("ascii"),
        )
        connection = {"id": 7, "provider": "google", "void": False, "refresh_token": encrypt_secret(flags.token_key, "refresh")}
        sent = []
        events = []

        class Repo:
            def match_notice(self, match_id):
                # The match has finished since the 'live' notice was queued.
                return _match(status=3, outcome=1, net_points=4)

            def active_subscriptions(self):
                return []

            def active_connections(self):
                return [connection]

            def event_map(self, connection_id, match_id):
                return None

            def save_event_map(self, connection_id, match_id, external_id, digest):
                events.append(("map", external_id))

            def record_delivery(self, outbox_id, channel, target, status, detail):
                events.append(("delivery", channel, status))

            def mark_done(self, item_id):
                events.append(("done", item_id))

        def fake_request(method, url, json=None, headers=None):
            sent.append(json)
            return _Response(200, {"id": "cplmatch0000000012"})

        deliver_item(
            Repo(),
            self._item("live"),
            flags,
            post=lambda url, data=None, headers=None: _Response(200, {"access_token": "a"}),
            request=fake_request,
        )
        self.assertEqual(len(sent), 1)
        self.assertFalse(sent[0]["summary"].startswith("Live:"), sent[0]["summary"])
        self.assertIn(("done", 9), events)


class ConnectionManagementTests(unittest.TestCase):
    """An admin sees and disconnects only their own calendar connections."""

    def test_disconnect_revokes_the_google_grant_and_voids_the_connection(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import notify_routes

        key = Fernet.generate_key().decode("ascii")
        flags = _flags(
            google_enabled=True,
            google_client_id="id",
            google_client_secret="secret",
            oauth_redirect_base="https://api.example",
            token_key=key,
        )
        stored = encrypt_secret(key, "refresh-value")
        calls = []

        class Repo:
            def __init__(self, db):
                pass

            def connection_for_admin(self, connection_id, admin_id):
                if (connection_id, admin_id) == (7, 1):
                    return {"id": 7, "provider": "google", "status": "active", "refresh_token": stored}
                return None

            def disconnect_connection(self, connection_id, admin_id):
                calls.append(("disconnect", connection_id, admin_id))
                return True

        def fake_post(url, data=None, headers=None):
            calls.append(("revoke", url, data))
            return _Response(200)

        with mock.patch.object(notify_routes, "NotifyRepository", Repo), \
                mock.patch.object(notify_routes, "_flags", return_value=flags), \
                mock.patch.object(notify_routes, "_http_post", side_effect=fake_post):
            notify_routes.disconnect(7, admin=SimpleNamespace(id=1), db=None)

        self.assertIn(("revoke", notify_routes.GOOGLE_REVOKE, {"token": "refresh-value"}), calls)
        self.assertIn(("disconnect", 7, 1), calls)

    def test_another_admins_connection_is_reported_as_not_found(self):
        from types import SimpleNamespace
        from unittest import mock

        from fastapi import HTTPException

        from routes import notify_routes

        class Repo:
            def __init__(self, db):
                pass

            def connection_for_admin(self, connection_id, admin_id):
                return None

            def disconnect_connection(self, connection_id, admin_id):
                raise AssertionError("must not disconnect someone else's connection")

        with mock.patch.object(notify_routes, "NotifyRepository", Repo), \
                mock.patch.object(notify_routes, "_http_post", side_effect=AssertionError("no revoke call")):
            with self.assertRaises(HTTPException) as raised:
                notify_routes.disconnect(7, admin=SimpleNamespace(id=2), db=None)
        self.assertEqual(raised.exception.status_code, 404)

    def test_a_revoked_grant_is_listed_as_needing_reauthorization(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import notify_routes

        class Repo:
            def __init__(self, db):
                pass

            def connections_for_admin(self, admin_id):
                return [
                    {"id": 7, "provider": "google", "status": "revoked", "updated_at": None},
                    {"id": 8, "provider": "microsoft", "status": "active", "updated_at": None},
                ]

        with mock.patch.object(notify_routes, "NotifyRepository", Repo):
            response = notify_routes.list_connections(admin=SimpleNamespace(id=1), db=None)
        states = {item["provider"]: item["state"] for item in response.data}
        self.assertEqual(states, {"google": "reauthorization_required", "microsoft": "connected"})


class FeedTokenTests(unittest.TestCase):
    """A feed link is a capability: unknown or revoked tokens get nothing, and tokens are high-entropy."""

    def _request(self):
        from starlette.requests import Request

        return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
                        "client": ("127.0.0.1", 1), "server": ("api.example", 443), "scheme": "https"})

    def test_an_unknown_or_revoked_feed_returns_not_found(self):
        from unittest import mock

        from fastapi import HTTPException

        from routes import notify_routes

        class Repo:
            def __init__(self, db):
                pass

            def feed_by_token(self, token):
                return None  # revoked feeds are voided, so the lookup finds nothing

        flags = _flags(calendar_feed_enabled=True)
        with mock.patch.object(notify_routes, "NotifyRepository", Repo), \
                mock.patch.object(notify_routes, "_flags", return_value=flags):
            with self.assertRaises(HTTPException) as raised:
                notify_routes.season_feed("not-a-real-token", self._request(), db=None)
        self.assertEqual(raised.exception.status_code, 404)

    def test_a_new_feed_gets_a_random_token_of_at_least_256_bits(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import notify_routes

        created = {}

        class Repo:
            def __init__(self, db):
                pass

            def season_exists(self, season_id):
                return True

            def create_feed(self, season_id, team_id, token, label):
                created["token"] = token
                return {}

        flags = _flags(calendar_feed_enabled=True)
        body = SimpleNamespace(season_id=3, team_id=None)
        with mock.patch.object(notify_routes, "NotifyRepository", Repo), \
                mock.patch.object(notify_routes, "_flags", return_value=flags):
            response = notify_routes.create_feed(body, self._request(), db=None)
        # token_urlsafe(32) gives 43 characters of base64url, which is 256 bits of randomness.
        self.assertGreaterEqual(len(created["token"]), 43)
        self.assertEqual(response.data["token"], created["token"])


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        from services.notify import telemetry

        telemetry.reset()

    def test_labels_are_sanitized_and_unknown_channels_are_grouped(self):
        from services.notify import telemetry

        telemetry.count("delivery.failed", "google", "token=abc def/ghi")
        telemetry.count("delivery.sent", "someothervendor")
        counts = telemetry.snapshot()
        self.assertEqual(counts["delivery.failed.google.token_abc_def_ghi"], 1)
        self.assertEqual(counts["delivery.sent.other"], 1)
        self.assertFalse([key for key in counts if "=" in key or "/" in key or " " in key])

    def test_a_revoked_grant_is_counted_and_visible_to_an_admin_only(self):
        from types import SimpleNamespace

        from routes import notify_routes
        from services.notify import telemetry

        flags = _flags(
            google_enabled=True,
            google_client_id="id",
            google_client_secret="secret",
            oauth_redirect_base="https://api.example",
            token_key=Fernet.generate_key().decode("ascii"),
        )
        connection = {"id": 7, "provider": "google", "void": False, "refresh_token": encrypt_secret(flags.token_key, "r")}

        class Repo:
            def match_notice(self, match_id):
                return _match()

            def active_subscriptions(self):
                return []

            def active_connections(self):
                return [connection]

            def event_map(self, connection_id, match_id):
                return None

            def record_delivery(self, *args):
                pass

            def set_connection_status(self, connection_id, status):
                pass

            def mark_done(self, item_id):
                pass

        deliver_item(
            Repo(),
            {"Id": 9, "MatchId": 12, "EventType": "scheduled", "Attempts": 1,
             "Payload": '{"event":"scheduled","match":{"id":12},"before":null}'},
            flags,
            post=lambda url, data=None, headers=None: _Response(400, {"error": "invalid_grant"}),
            request=lambda *args, **kwargs: None,
        )
        self.assertEqual(telemetry.snapshot().get("connection.revoked.google"), 1)
        self.assertEqual(telemetry.snapshot().get("delivery.failed.google.reauthorization_required"), 1)

        response = notify_routes.notify_telemetry(admin=SimpleNamespace(id=1))
        self.assertEqual(response.data["process_counts"]["connection.revoked.google"], 1)

    def test_the_admin_view_includes_backlog_and_failure_counts_from_the_database(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import notify_routes

        class Repo:
            def __init__(self, db):
                pass

            def delivery_health(self):
                return {"backlog": 3, "dead": 1, "oldest_pending_seconds": 42, "active_subscriptions": 5,
                        "active_connections": 2, "connections_needing_reauth": 1, "failed_last_24h": 4, "sent_last_24h": 9}

        with mock.patch.object(notify_routes, "NotifyRepository", Repo):
            response = notify_routes.notify_telemetry(admin=SimpleNamespace(id=1), db=None)
        self.assertEqual(response.data["delivery_health"]["backlog"], 3)
        self.assertEqual(response.data["delivery_health"]["connections_needing_reauth"], 1)


try:
    import icalendar
except ImportError:  # pragma: no cover - the parse test needs the optional icalendar package
    icalendar = None


@unittest.skipUnless(icalendar, "pip install icalendar to run the RFC 5545 parse test")
class IcsParseTests(unittest.TestCase):
    """A real iCalendar parser reads the feed the way calendar apps do."""

    def _parsed(self, *matches):
        body = calendar_text(list(matches), "Carrom Premier League", "https://league.example",
                             now=datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc))
        return icalendar.Calendar.from_ical(body), body

    def test_the_feed_parses_with_one_event_per_match(self):
        long_names = _match(id=13, team1_name="Ünïcode Strikers " * 6, team2_name="Queen Coverers " * 6)
        calendar, body = self._parsed(_match(), long_names, _match(id=14, void=True))
        events = calendar.walk("VEVENT")
        self.assertEqual([str(event["UID"]) for event in events],
                         [event_uid(12), event_uid(13), event_uid(14)])
        self.assertTrue(all(len(line.encode("utf-8")) <= 75 for line in body.split("\r\n")))
        self.assertIn("Ünïcode Strikers", str(events[1]["SUMMARY"]))
        self.assertEqual(str(events[2]["STATUS"]), "CANCELLED")

    def test_each_event_is_an_all_day_date_with_sequence_and_last_modified(self):
        calendar, _ = self._parsed(_match())
        event = calendar.walk("VEVENT")[0]
        self.assertEqual(event.decoded("DTSTART"), date(2026, 11, 18))
        self.assertEqual(event.decoded("DTEND"), date(2026, 11, 19))
        self.assertEqual(int(event["SEQUENCE"]), sequence_for(_match()))
        self.assertEqual(event.decoded("LAST-MODIFIED"), datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))

    def test_a_later_edit_raises_the_sequence_so_calendars_replace_the_event(self):
        earlier = self._parsed(_match())[0].walk("VEVENT")[0]
        later = self._parsed(_match(updated_at=datetime(2026, 11, 5, 10, 0, tzinfo=timezone.utc)))[0].walk("VEVENT")[0]
        self.assertEqual(str(earlier["UID"]), str(later["UID"]))
        self.assertGreater(int(later["SEQUENCE"]), int(earlier["SEQUENCE"]))

    def test_the_reminder_fires_at_nine_in_the_morning_india_time(self):
        from zoneinfo import ZoneInfo

        alarm = self._parsed(_match())[0].walk("VALARM")[0]
        fires = alarm.decoded("TRIGGER")
        self.assertEqual(fires.astimezone(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None), datetime(2026, 11, 18, 9, 0))


class TimeZoneTests(unittest.TestCase):
    def test_the_league_day_turns_over_at_midnight_india_time_not_utc(self):
        from unittest import mock

        from services import match_rules

        late_utc = datetime(2026, 11, 18, 18, 45, tzinfo=timezone.utc)  # 00:15 on the 19th in India

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return late_utc.astimezone(tz) if tz else late_utc.replace(tzinfo=None)

        with mock.patch.object(match_rules, "datetime", Clock):
            self.assertEqual(match_rules.league_today(), date(2026, 11, 19))

    def test_sequence_is_the_same_for_the_same_instant_in_any_zone(self):
        from datetime import timedelta as delta

        utc = _match(updated_at=datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc))
        india = _match(updated_at=datetime(2026, 11, 1, 15, 30, tzinfo=timezone(delta(hours=5, minutes=30))))
        naive = _match(updated_at=datetime(2026, 11, 1, 10, 0))
        text = _match(updated_at="2026-11-01T10:00:00Z")
        self.assertEqual({sequence_for(m) for m in (utc, india, naive, text)}, {sequence_for(utc)})

    def test_provider_events_stay_on_the_match_date_whatever_the_update_time(self):
        late_edit = _match(updated_at=datetime(2026, 11, 17, 23, 30, tzinfo=timezone.utc))
        google = google_event_body(late_edit, "https://league.example")
        microsoft = microsoft_event_body(late_edit, "https://league.example")
        self.assertEqual((google["start"]["date"], google["end"]["date"]), ("2026-11-18", "2026-11-19"))
        self.assertEqual(microsoft["start"], {"dateTime": "2026-11-18T00:00:00", "timeZone": "Asia/Kolkata"})
        self.assertTrue(microsoft["isAllDay"])


class CalendarBackfillTests(unittest.TestCase):
    def test_a_sync_notice_writes_only_to_the_new_calendar_and_never_pushes(self):
        flags = _flags(
            push_enabled=True, vapid_public_key="pub", vapid_private_key="priv", vapid_subject="mailto:a@b.c",
            google_enabled=True, google_client_id="id", google_client_secret="secret",
            oauth_redirect_base="https://api.example", token_key=Fernet.generate_key().decode("ascii"),
        )
        connections = [{"id": 7, "provider": "google"}, {"id": 8, "provider": "google"}]
        subscriptions = [{"id": 1, "void": False}]
        actions = delivery_plan("sync", _match(), subscriptions, connections, flags, only_connection=8)
        self.assertEqual([(a["channel"], a["target"]) for a in actions], [("google", 8)])
        self.assertEqual(delivery_plan("sync", _match(), subscriptions, connections, flags), [])

    def test_connecting_queues_each_upcoming_match_once_for_that_calendar(self):
        from unittest import mock

        from services.notify import enqueue as enqueue_module

        queued = []

        class Repo:
            def __init__(self, db):
                pass

            def upcoming_matches(self, day):
                return [_match(id=12), _match(id=13)]

            def enqueue(self, match, event, key=None, connection_id=None):
                if key in {k for k, _ in queued}:
                    return None
                queued.append((key, connection_id))
                return len(queued)

        with mock.patch.object(enqueue_module, "NotifyRepository", Repo):
            self.assertEqual(enqueue_module.backfill_connection(None, 8, date(2026, 11, 18)), 2)
            self.assertEqual(enqueue_module.backfill_connection(None, None, date(2026, 11, 18)), 0)
        self.assertEqual(queued, [("12:sync:8", 8), ("13:sync:8", 8)])


class SyncStateTests(unittest.TestCase):
    def test_each_connection_shows_where_its_updates_stand(self):
        from routes.notify_routes import sync_state

        self.assertEqual(sync_state("revoked", {"last_status": "sent"}), "reauthorization_required")
        self.assertEqual(sync_state("active", {"last_status": "failed", "queue_pending": True}), "sync_failed")
        self.assertEqual(sync_state("active", {"last_status": "sent", "queue_pending": True}), "update_pending")
        self.assertEqual(sync_state("active", {"last_status": "sent", "queue_pending": False}), "synced")
        self.assertEqual(sync_state("active", {"last_status": "skipped"}), "synced")
        self.assertEqual(sync_state("active", None), "connected")


class RetrySafetyTests(unittest.TestCase):
    def _flags(self):
        return _flags(push_enabled=True, vapid_public_key="pub", vapid_private_key="priv", vapid_subject="mailto:a@b.c")

    def test_a_retry_skips_devices_that_already_got_the_alert(self):
        from unittest import mock

        sent_to = []
        recorded = []

        class Repo:
            def match_notice(self, match_id):
                return _match()

            def active_subscriptions(self):
                return [{"id": 1, "endpoint": "https://push.example/1", "void": False},
                        {"id": 2, "endpoint": "https://push.example/2", "void": False}]

            def active_connections(self):
                return []

            def sent_targets(self, outbox_id, channel):
                return {"1"}

            def record_delivery(self, outbox_id, channel, target, status, detail):
                recorded.append((target, status))

            def mark_done(self, item_id):
                pass

        def fake_push(subscription, body, **kwargs):
            sent_to.append(subscription["id"])
            return {"ok": True, "blocked": False, "gone": False, "reason": ""}

        with mock.patch.object(worker_module, "send_web_push", side_effect=fake_push):
            deliver_item(Repo(), {"Id": 9, "MatchId": 12, "EventType": "live", "Attempts": 2,
                                  "Payload": '{"event":"live","match":{"id":12},"before":null}'},
                         self._flags(), post=None, request=None)
        self.assertEqual(sent_to, [2])
        self.assertEqual(recorded, [("2", "sent")])

    def test_an_unexpected_failure_stores_only_the_error_type(self):
        from unittest import mock

        recorded = []

        class Repo:
            def match_notice(self, match_id):
                return _match()

            def active_subscriptions(self):
                return [{"id": 1, "endpoint": "https://push.example/secret-endpoint-path", "void": False}]

            def active_connections(self):
                return []

            def record_delivery(self, outbox_id, channel, target, status, detail):
                recorded.append(detail)

            def mark_retry(self, *args):
                pass

        def exploding_push(subscription, body, **kwargs):
            raise ValueError("leaked https://push.example/secret-endpoint-path and a provider body")

        with mock.patch.object(worker_module, "send_web_push", side_effect=exploding_push):
            deliver_item(Repo(), {"Id": 9, "MatchId": 12, "EventType": "live", "Attempts": 1,
                                  "Payload": '{"event":"live","match":{"id":12},"before":null}'},
                         self._flags(), post=None, request=None)
        self.assertEqual(recorded, ["unexpected: ValueError"])


class SubscriptionRotationTests(unittest.TestCase):
    def test_a_rotated_endpoint_keeps_the_old_choices_and_retires_the_old_row(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import notify_routes
        from schemas.notify_schema import PushSubscribeRequest

        saved = []
        voided = []
        old = {"id": 4, "endpoint": "https://push.example/old", "void": False, "admin_id": None,
               "season_id": 3, "team_id": 2, "notify_scheduled": True, "notify_changed": False,
               "notify_cancelled": True, "notify_live": False, "notify_ended": True, "notify_score": False,
               "notify_reminder": True}

        class Repo:
            def __init__(self, db):
                pass

            def subscription_by_endpoint(self, endpoint):
                return old if endpoint == old["endpoint"] else None

            def save_subscription(self, row):
                saved.append(row)
                return {**row, "id": 5, "void": False}

            def void_subscription(self, endpoint):
                voided.append(endpoint)
                return True

        flags = _flags(push_enabled=True, vapid_public_key="pub", vapid_private_key="priv", vapid_subject="mailto:a@b.c")
        body = PushSubscribeRequest(endpoint="https://push.example/new", p256dh="p", auth="a",
                                    replaces_endpoint="https://push.example/old")
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"))
        with mock.patch.object(notify_routes, "NotifyRepository", Repo), \
                mock.patch.object(notify_routes, "_flags", return_value=flags):
            notify_routes.subscribe(body, request, db=None, admin=None)
        self.assertEqual(saved[0]["endpoint"], "https://push.example/new")
        self.assertEqual((saved[0]["team_id"], saved[0]["notify_live"], saved[0]["notify_score"]), (2, False, False))
        self.assertEqual(voided, ["https://push.example/old"])

    def test_a_second_report_of_the_same_rotation_keeps_the_carried_over_choices(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import notify_routes
        from schemas.notify_schema import PushSubscribeRequest

        rotated = {"id": 5, "endpoint": "https://push.example/new", "void": False, "admin_id": None,
                   "season_id": 3, "team_id": 2, "notify_scheduled": True, "notify_changed": False,
                   "notify_cancelled": True, "notify_live": False, "notify_ended": True, "notify_score": False,
                   "notify_reminder": True}

        class Repo:
            def __init__(self, db):
                pass

            def subscription_by_endpoint(self, endpoint):
                if endpoint == rotated["endpoint"]:
                    return rotated
                return {**rotated, "id": 4, "endpoint": endpoint, "void": True}

            def save_subscription(self, row):
                raise AssertionError("the rotated row must not be overwritten")

        flags = _flags(push_enabled=True, vapid_public_key="pub", vapid_private_key="priv", vapid_subject="mailto:a@b.c")
        body = PushSubscribeRequest(endpoint="https://push.example/new", p256dh="p", auth="a",
                                    replaces_endpoint="https://push.example/old", notify_live=True)
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"))
        with mock.patch.object(notify_routes, "NotifyRepository", Repo), \
                mock.patch.object(notify_routes, "_flags", return_value=flags):
            response = notify_routes.subscribe(body, request, db=None, admin=None)
        self.assertEqual(response.data["id"], 5)

    def test_rate_limits_key_on_the_visitor_behind_the_proxy(self):
        from types import SimpleNamespace

        from routes.notify_routes import _client_key

        behind_proxy = SimpleNamespace(headers={"x-forwarded-for": "198.51.100.7, 10.0.0.1"}, client=SimpleNamespace(host="10.0.0.1"))
        direct = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"))
        self.assertEqual(_client_key(behind_proxy), "198.51.100.7")
        self.assertEqual(_client_key(direct), "203.0.113.9")


class EndpointKeyTests(unittest.TestCase):
    def test_long_push_endpoints_get_a_fixed_size_key(self):
        from repositories.notify_repository import endpoint_hash

        long_endpoint = "https://push.example/" + "x" * 1979
        self.assertEqual(len(endpoint_hash(long_endpoint)), 64)
        self.assertNotEqual(endpoint_hash(long_endpoint), endpoint_hash(long_endpoint + "y"))


def _ready_flags(**overrides) -> NotifyFlags:
    base = dict(
        push_enabled=True, vapid_public_key="pub", vapid_private_key="priv", vapid_subject="mailto:a@b.c",
        google_enabled=True, google_client_id="id", google_client_secret="secret",
        oauth_redirect_base="https://api.example", token_key=Fernet.generate_key().decode("ascii"),
    )
    base.update(overrides)
    return _flags(**base)


class LiveRolloutFlagTests(unittest.TestCase):
    connections = [{"id": 7, "provider": "google"}]
    subscriptions = [{"id": 1, "void": False}]

    def test_both_live_switches_default_off(self):
        flags = load_flags({})
        self.assertFalse(flags.push_live_enabled)
        self.assertFalse(flags.calendar_live_updates_enabled)
        flags = load_flags({"CPL_PUSH_LIVE_ENABLED": "true", "CPL_CALENDAR_LIVE_UPDATES_ENABLED": "on"})
        self.assertTrue(flags.push_live_enabled)
        self.assertTrue(flags.calendar_live_updates_enabled)

    def test_match_started_pushes_wait_for_their_own_switch(self):
        flags = _ready_flags(push_live_enabled=False)
        actions = delivery_plan("live", _match(status=2), self.subscriptions, self.connections, flags)
        self.assertIn(("push", "skipped"), [(a["channel"], a["status"]) for a in actions])
        self.assertNotIn(("push", "send"), [(a["channel"], a["status"]) for a in actions])
        ended = delivery_plan("ended", _match(status=3), self.subscriptions, self.connections, flags)
        self.assertIn(("push", "send"), [(a["channel"], a["status"]) for a in ended])

    def test_live_and_result_calendar_writes_wait_for_their_own_switch(self):
        flags = _ready_flags(calendar_live_updates_enabled=False)
        for event in ("live", "ended", "score"):
            actions = delivery_plan(event, _match(status=3), self.subscriptions, self.connections, flags)
            google = [(a["status"], a.get("reason")) for a in actions if a["channel"] == "google"]
            self.assertEqual(google, [("skipped", "CPL_CALENDAR_LIVE_UPDATES_ENABLED is off.")], event)
        moved = delivery_plan("rescheduled", _match(), self.subscriptions, self.connections, flags)
        self.assertIn(("google", "send"), [(a["channel"], a["status"]) for a in moved])


class ScheduleRebuildTests(unittest.TestCase):
    """POST /seasons/{id}/schedule deletes unplayed matches and inserts new ones."""

    connections = [{"id": 7, "provider": "google"}]
    subscriptions = [{"id": 1, "void": False, "team_id": 99, "season_id": 3}]

    def test_a_calendar_notice_never_pushes_and_a_season_alert_never_writes_calendars(self):
        flags = _ready_flags()
        calendar = delivery_plan("calendar", _match(), self.subscriptions, self.connections, flags)
        self.assertEqual([(a["channel"], a["status"]) for a in calendar], [("google", "send")])
        season = delivery_plan("schedule", _match(), self.subscriptions, self.connections, flags)
        # A team follower hears about the season rebuild even when their team is not in the first match.
        self.assertEqual([(a["channel"], a["status"]) for a in season], [("push", "send")])
        other_season = [{"id": 2, "void": False, "season_id": 4}]
        self.assertEqual(delivery_plan("schedule", _match(), other_season, [], flags), [])

    def test_the_season_alert_links_to_the_schedule_and_collapses_per_season(self):
        payload = push_payload("schedule", _match(), {"schedule_count": 6}, "https://league.example", 41)
        self.assertEqual(payload["url"], "https://league.example/schedule")
        self.assertEqual(payload["tag"], "season-3-schedule")
        self.assertIsNone(payload["matchId"])
        self.assertEqual(payload["noticeId"], 41)
        self.assertIn("6 matches", payload["body"])
        self.assertEqual(payload["title"], "Schedule updated")

    def _repo(self, before, after, queued):
        calls = {"n": 0}

        class Repo:
            def __init__(self, db):
                pass

            def season_scheduled(self, season_id):
                calls["n"] += 1
                return before if calls["n"] == 1 else after

            def enqueue(self, match, event, before=None, key=None, connection_id=None):
                if key in {item[2] for item in queued}:
                    return None
                queued.append((int(match["id"]), event, key, bool(match.get("void")), before))
                return len(queued)

        return Repo

    def test_the_route_withdraws_removed_matches_and_writes_new_ones(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import season_routes
        from services.notify import enqueue as enqueue_module

        queued = []
        before = [_match(id=10), _match(id=11)]
        after = [_match(id=20), _match(id=21)]

        class Controller:
            def __init__(self, db):
                pass

            def schedule_matches(self, season_id):
                return SimpleNamespace(removed=2, created=2)

        with mock.patch.object(enqueue_module, "NotifyRepository", self._repo(before, after, queued)), \
                mock.patch.object(enqueue_module, "load_flags", return_value=_ready_flags()), \
                mock.patch.object(season_routes, "SeasonController", Controller):
            response = season_routes.schedule_season_matches(3, db=None)
        self.assertTrue(response.success)
        self.assertEqual(
            [(match_id, event, void) for match_id, event, _, void, _ in queued],
            [(10, "calendar", True), (11, "calendar", True), (20, "calendar", False), (21, "calendar", False), (20, "schedule", False)],
        )
        self.assertEqual(queued[-1][4], {"schedule_count": 2})

    def test_a_failed_rebuild_queues_nothing_and_flags_off_reads_nothing(self):
        from unittest import mock

        from fastapi import HTTPException

        from routes import season_routes
        from services.notify import enqueue as enqueue_module

        queued = []

        class Controller:
            def __init__(self, db):
                pass

            def schedule_matches(self, season_id):
                raise HTTPException(status_code=400, detail="Add teams first.")

        with mock.patch.object(enqueue_module, "NotifyRepository", self._repo([_match(id=10)], [], queued)), \
                mock.patch.object(enqueue_module, "load_flags", return_value=_ready_flags()), \
                mock.patch.object(season_routes, "SeasonController", Controller):
            with self.assertRaises(HTTPException):
                season_routes.schedule_season_matches(3, db=None)
        self.assertEqual(queued, [])

        class Untouchable:
            def __init__(self, db):
                raise AssertionError("no reads while the flags are off")

        with mock.patch.object(enqueue_module, "NotifyRepository", Untouchable), \
                mock.patch.object(enqueue_module, "load_flags", return_value=_flags()):
            self.assertIsNone(enqueue_module.season_scheduled(None, 3))
            self.assertEqual(enqueue_module.publish_schedule_rebuild(None, 3, None), 0)

    def test_a_queue_failure_does_not_fail_the_rebuild(self):
        from types import SimpleNamespace
        from unittest import mock

        from routes import season_routes
        from services.notify import enqueue as enqueue_module

        class Broken:
            def __init__(self, db):
                pass

            def season_scheduled(self, season_id):
                raise RuntimeError("outbox table missing")

        class Controller:
            def __init__(self, db):
                pass

            def schedule_matches(self, season_id):
                return SimpleNamespace(removed=0, created=4)

        with mock.patch.object(enqueue_module, "NotifyRepository", Broken), \
                mock.patch.object(enqueue_module, "load_flags", return_value=_ready_flags()), \
                mock.patch.object(season_routes, "SeasonController", Controller):
            response = season_routes.schedule_season_matches(3, db=None)
        self.assertTrue(response.success)

    def test_a_deleted_match_is_withdrawn_from_the_calendar_from_its_snapshot(self):
        from unittest import mock

        written = []

        class Repo:
            def match_notice(self, match_id):
                return None  # the rebuild deleted the row

            def active_subscriptions(self):
                return [{"id": 1, "void": False}]

            def active_connections(self):
                return [{"id": 7, "provider": "google"}]

            def mark_done(self, item_id):
                pass

        def fake_google(repository, item, match, connection, flags, post, request):
            written.append((match["id"], match["void"]))

        payload = '{"event":"calendar","match":{"id":10,"season_id":3,"void":true},"before":null}'
        with mock.patch.object(worker_module, "_deliver_google", side_effect=fake_google), \
                mock.patch.object(worker_module, "send_web_push", side_effect=AssertionError("no push")):
            deliver_item(Repo(), {"Id": 9, "MatchId": 10, "EventType": "calendar", "Attempts": 1, "Payload": payload},
                         _ready_flags(), post=None, request=None)
        self.assertEqual(written, [(10, True)])

    def test_a_late_retry_for_a_deleted_match_cannot_restore_its_event(self):
        from unittest import mock

        written = []

        class Repo:
            def match_notice(self, match_id):
                return None

            def active_subscriptions(self):
                return []

            def active_connections(self):
                return [{"id": 7, "provider": "google"}]

            def mark_done(self, item_id):
                pass

        def fake_google(repository, item, match, connection, flags, post, request):
            written.append(match["void"])

        class Devices(Repo):
            def active_subscriptions(self):
                return [{"id": 1, "void": False}]

            def sent_targets(self, outbox_id, channel):
                return set()

        older = '{"event":"scheduled","match":{"id":10,"season_id":3,"void":false},"before":null}'
        with mock.patch.object(worker_module, "_deliver_google", side_effect=fake_google), \
                mock.patch.object(worker_module, "send_web_push", side_effect=AssertionError("no stale alert")):
            deliver_item(Devices(), {"Id": 8, "MatchId": 10, "EventType": "scheduled", "Attempts": 3, "Payload": older},
                         _ready_flags(), post=None, request=None)
        self.assertEqual(written, [True])


class MatchRouteWiringTests(unittest.TestCase):
    """Each match write queues its notice after the write, and a queue failure never fails the write."""

    def _controller(self):
        from types import SimpleNamespace

        class Controller:
            service = SimpleNamespace(repository=SimpleNamespace(notices=[]))

            def create(self, request):
                return SimpleNamespace(id=12)

            def update(self, match_id, request):
                return SimpleNamespace(id=match_id)

            def reset(self, match_id):
                return SimpleNamespace(id=match_id)

            def delete(self, match_id):
                return True

        return Controller()

    def test_create_update_reset_and_delete_each_publish(self):
        from unittest import mock

        from routes import match_routes

        published = []
        notices = {"value": _match()}
        with mock.patch.object(match_routes, "_armed", return_value=True), \
                mock.patch.object(match_routes, "load_notice", side_effect=lambda db, match_id: notices["value"]), \
                mock.patch.object(match_routes, "publish_side_effects"), \
                mock.patch.object(match_routes, "publish_match_change",
                                  side_effect=lambda db, kind, before, after: published.append((kind, before is not None, after))):
            match_routes.create(None, db=None, controller=self._controller())
            match_routes.update(12, None, db=None, controller=self._controller())
            match_routes.reset(12, db=None, controller=self._controller())
            notices["value"] = None
            match_routes.delete(12, db=None, controller=self._controller())
        self.assertEqual([kind for kind, _, _ in published], ["created", "updated", "reset", "deleted"])
        self.assertFalse(published[0][1])
        self.assertTrue(published[1][1])

    def test_a_deleted_match_is_published_as_void(self):
        from unittest import mock

        from routes import match_routes

        published = []
        reads = iter([_match(), None])
        with mock.patch.object(match_routes, "_armed", return_value=True), \
                mock.patch.object(match_routes, "load_notice", side_effect=lambda db, match_id: next(reads)), \
                mock.patch.object(match_routes, "publish_side_effects"), \
                mock.patch.object(match_routes, "publish_match_change",
                                  side_effect=lambda db, kind, before, after: published.append(after)):
            match_routes.delete(12, db=None, controller=self._controller())
        self.assertTrue(published[0]["void"])

    def test_flags_off_reads_and_queues_nothing(self):
        from unittest import mock

        from routes import match_routes

        with mock.patch.object(match_routes, "_armed", return_value=False), \
                mock.patch.object(match_routes, "load_notice", side_effect=AssertionError("no read")), \
                mock.patch.object(match_routes, "publish_match_change", side_effect=AssertionError("no queue")):
            response = match_routes.update(12, None, db=None, controller=self._controller())
        self.assertTrue(response.success)

    def test_an_outbox_failure_leaves_the_match_write_successful(self):
        from unittest import mock

        from routes import match_routes
        from services.notify import enqueue as enqueue_module

        class Broken:
            def __init__(self, db):
                pass

            def match_notice(self, match_id):
                return _match()

            def enqueue(self, *args, **kwargs):
                raise RuntimeError("Invalid object name 'tblNotificationOutbox'")

        with mock.patch.object(match_routes, "_armed", return_value=True), \
                mock.patch.object(enqueue_module, "NotifyRepository", Broken), \
                mock.patch.object(enqueue_module, "load_flags", return_value=_ready_flags()):
            response = match_routes.create(None, db=None, controller=self._controller())
        self.assertTrue(response.success)
        self.assertEqual(response.data.id, 12)


class PkceTests(unittest.TestCase):
    def test_the_verifier_is_valid_and_bound_to_the_state(self):
        import re

        from services.notify.security import pkce_challenge, pkce_verifier

        verifier = pkce_verifier("secret", "state-a")
        self.assertRegex(verifier, re.compile(r"^[A-Za-z0-9_-]{43,128}$"))
        self.assertEqual(verifier, pkce_verifier("secret", "state-a"))
        self.assertNotEqual(verifier, pkce_verifier("secret", "state-b"))
        self.assertNotEqual(verifier, pkce_verifier("other", "state-a"))
        # RFC 7636 appendix B test vector.
        self.assertEqual(pkce_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_connect_sends_the_challenge_and_the_callback_sends_the_verifier(self):
        from types import SimpleNamespace
        from urllib.parse import parse_qs, urlparse
        from unittest import mock

        from core.config import settings
        from routes import notify_routes
        from services.notify.security import pkce_challenge, pkce_verifier

        flags = _ready_flags()
        url = notify_routes._connect("google", SimpleNamespace(id=5), flags)["authorize_url"]
        query = parse_qs(urlparse(url).query)
        state = query["state"][0]
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], [pkce_challenge(pkce_verifier(settings.AUTH_JWT_SECRET, state))])

        sent = {}

        def fake_exchange(flags, code, post, verifier=None):
            sent["verifier"] = verifier
            return {"refresh_token": "refresh"}

        class Repo:
            def __init__(self, db):
                pass

            def save_connection(self, admin_id, provider, stored, calendar_id):
                return 3

        with mock.patch.object(notify_routes, "_flags", return_value=flags), \
                mock.patch.object(notify_routes, "exchange_google_code", side_effect=fake_exchange), \
                mock.patch.object(notify_routes, "NotifyRepository", Repo), \
                mock.patch.object(notify_routes, "backfill_connection", return_value=0):
            response = notify_routes._finish_oauth("google", "code", state, db=None)
        self.assertIn("calendar=google", response.headers["location"])
        self.assertEqual(sent["verifier"], pkce_verifier(settings.AUTH_JWT_SECRET, state))


class FeedTokenStorageTests(unittest.TestCase):
    def test_feed_tokens_are_stored_hashed_and_looked_up_by_hash(self):
        from repositories.notify_repository import NotifyRepository, token_hash

        executed = []

        class Cursor:
            rowcount = 1

            def execute(self, sql, params=()):
                executed.append(params)

            def fetchone(self):
                return None

        class Db:
            def cursor(self, dictionary=False):
                return Cursor()

            def commit(self):
                pass

        repository = NotifyRepository(Db())
        repository.create_feed(3, None, "plain-token-value", "Season 3")
        repository.feed_by_token("plain-token-value")
        repository.revoke_feed("plain-token-value")
        hashed = token_hash("plain-token-value")
        self.assertEqual(executed[0][0], hashed)
        self.assertEqual(executed[1], (hashed,))
        self.assertEqual(executed[2], (hashed,))
        self.assertNotIn("plain-token-value", repr(executed))

    def test_access_logs_never_show_a_feed_token(self):
        import logging

        from services.notify.redact import FeedTokenFilter, install_feed_token_filter, redact_feed_tokens

        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
                                   ("203.0.113.9:1", "GET", "/calendar/feeds/AbC-123_xyz.ics?x=1", "1.1", 200), None)
        FeedTokenFilter().filter(record)
        self.assertNotIn("AbC-123_xyz", record.getMessage())
        self.assertIn("/calendar/feeds/[redacted]", record.getMessage())
        self.assertEqual(redact_feed_tokens("DELETE /calendar/feeds/secret"), "DELETE /calendar/feeds/[redacted]")
        self.assertEqual(redact_feed_tokens("/calendar/matches/12.ics"), "/calendar/matches/12.ics")
        install_feed_token_filter()
        install_feed_token_filter()
        access = logging.getLogger("uvicorn.access")
        self.assertEqual(sum(isinstance(item, FeedTokenFilter) for item in access.filters), 1)


if __name__ == "__main__":
    unittest.main()

