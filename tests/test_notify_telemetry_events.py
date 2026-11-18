"""Telemetry for attempts by notification type, push acceptance, calendar event outcomes, and sync latency."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from tests import test_auth_service as _env  # noqa: F401  (sets required app env before config imports)

from tests.test_notify import _match, _ready_flags


class CalendarOutcomeTests(unittest.TestCase):
    def setUp(self):
        from services.notify import telemetry

        telemetry.reset()

    def test_a_first_write_counts_created_and_a_later_one_updated(self):
        from services.notify import telemetry, worker

        item = {"CreatedAt": datetime.now(timezone.utc) - timedelta(seconds=30)}
        worker._note_calendar_write(item, "google", _match(), existed=False)
        worker._note_calendar_write(item, "google", _match(), existed=True)
        counts = telemetry.snapshot()
        self.assertEqual(counts.get("calendar.event.google.created"), 1)
        self.assertEqual(counts.get("calendar.event.google.updated"), 1)
        latency = telemetry.latency()["calendar.sync_latency"]
        self.assertEqual(latency["count"], 2)
        self.assertGreaterEqual(latency["max_seconds"], 29)

    def test_a_voided_match_counts_cancelled(self):
        from services.notify import telemetry, worker

        worker._note_calendar_write({"CreatedAt": None}, "microsoft", _match(void=True), existed=True)
        self.assertEqual(telemetry.snapshot().get("calendar.event.microsoft.cancelled"), 1)
        self.assertNotIn("calendar.sync_latency", telemetry.latency())


class PushAcceptanceTests(unittest.TestCase):
    def test_an_accepted_push_is_counted_by_notification_type(self):
        from unittest import mock

        from services.notify import telemetry, worker

        telemetry.reset()

        class Repo:
            def note_push_success(self, subscription_id):
                pass

            def record_delivery(self, *args):
                pass

        item = {"Id": 4, "EventType": "rescheduled"}
        flags = _ready_flags()
        with mock.patch.object(worker, "send_web_push", return_value={"ok": True}), \
                mock.patch.object(worker, "push_payload", return_value={}):
            worker._deliver_push(Repo(), item, _match(), None, {"id": 9}, flags)
        counts = telemetry.snapshot()
        self.assertEqual(counts.get("push.accepted.rescheduled"), 1)
        self.assertEqual(counts.get("delivery.sent.push"), 1)


class AttemptCountTests(unittest.TestCase):
    def test_each_attempt_is_counted_by_channel_and_notification_type(self):
        from unittest import mock

        from services.notify import telemetry, worker

        telemetry.reset()

        class Repo:
            def match_notice(self, match_id):
                return _match(id=match_id)

            def active_subscriptions(self):
                return [{"id": 9, "admin_id": None}]

            def active_connections(self):
                return []

            def withdrawn_connection_ids(self, match_id):
                return set()

            def sent_targets(self, outbox_id, channel):
                return set()

            def record_delivery(self, *args):
                pass

            def mark_done(self, item_id):
                pass

        item = {
            "Id": 4,
            "MatchId": 12,
            "EventType": "live",
            "Attempts": 1,
            "Payload": json.dumps({"event": "live", "match": {"id": 12}}),
            "CreatedAt": datetime.now(timezone.utc),
        }
        action = {"channel": "push", "status": "send", "target": "9", "subscription": {"id": 9}}
        with mock.patch.object(worker, "delivery_plan", return_value=[action]), \
                mock.patch.object(worker, "_deliver_push"):
            worker.deliver_item(Repo(), item, _ready_flags(), None, None)
        self.assertEqual(telemetry.snapshot().get("delivery.attempt.push.live"), 1)


if __name__ == "__main__":
    unittest.main()
