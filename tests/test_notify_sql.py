"""SQL Server checks for the match-alert outbox, connections, and feeds.

These run only when CPL_NOTIFY_SQL_TEST_CONNECTION holds a full pyodbc
connection string for a disposable database that has run Setup-Database.ps1.
Never point it at a live database: the tests insert, update, and delete rows
they created themselves.

Example (Windows authentication, scratch database):
    set CPL_NOTIFY_SQL_TEST_CONNECTION=DRIVER={ODBC Driver 17 for SQL Server};SERVER=localhost;DATABASE=cpl_notify_scratch;Trusted_Connection=yes;TrustServerCertificate=yes
"""

from __future__ import annotations

import itertools
import os
import time
import unittest
from datetime import date

from tests import test_auth_service as _env  # noqa: F401  (sets required app env before config imports)

CONNECTION_STRING = os.environ.get("CPL_NOTIFY_SQL_TEST_CONNECTION", "")
_counter = itertools.count((int(time.time()) % 100000) * 100)


def _connect():
    import pyodbc

    from core.database import SqlServerConnection

    return SqlServerConnection(pyodbc.connect(CONNECTION_STRING, autocommit=False))


@unittest.skipUnless(CONNECTION_STRING, "CPL_NOTIFY_SQL_TEST_CONNECTION is not set")
class OutboxSqlTests(unittest.TestCase):
    def setUp(self):
        from repositories.notify_repository import NotifyRepository

        self.db = _connect()
        self.repo = NotifyRepository(self.db)
        self.season_id = self._insert_season()
        self.match_id = self._insert_match(self.season_id)

    def tearDown(self):
        cursor = self.db.cursor()
        cursor.execute(
            "DELETE FROM dbo.tblNotificationDeliveries WHERE OutboxId IN (SELECT Id FROM dbo.tblNotificationOutbox WHERE MatchId = %s)",
            (self.match_id,),
        )
        cursor.execute("DELETE FROM dbo.tblNotificationOutbox WHERE MatchId = %s", (self.match_id,))
        cursor.execute("DELETE FROM dbo.tblCalendarEventMaps WHERE MatchId = %s", (self.match_id,))
        cursor.execute("DELETE FROM dbo.tblPushSubscriptions WHERE SeasonId = %s", (self.season_id,))
        cursor.execute("DELETE FROM dbo.tblCalendarFeeds WHERE SeasonId = %s", (self.season_id,))
        cursor.execute("DELETE FROM dbo.tblMatches WHERE Id = %s", (self.match_id,))
        cursor.execute("DELETE FROM dbo.tblSeasons WHERE Id = %s", (self.season_id,))
        self.db.commit()
        self.db.close()

    def _insert_season(self) -> int:
        cursor = self.db.cursor()
        cursor.execute("INSERT INTO dbo.tblSeasons (Name, [Show]) VALUES (%s, 0)", (f"alerts-test-{next(_counter)}",))
        season_id = int(cursor.lastrowid)
        self.db.commit()
        return season_id

    def _insert_match(self, season_id: int) -> int:
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO dbo.tblMatches (ScheduledDate, Category, [Order], SeasonId, Status)
            VALUES (%s, 1, %s, %s, 1)
            """,
            (date(2026, 11, 20), next(_counter), season_id),
        )
        match_id = int(cursor.lastrowid)
        self.db.commit()
        return match_id

    def test_one_google_event_follows_a_match_from_schedule_to_final_result_and_cancel(self):
        """The whole path on a real database: match write, outbox, worker, and a fake Google API.

        Scenarios B to E in the goal, end to end. Only the provider is faked.
        """
        from unittest import mock

        from cryptography.fernet import Fernet

        from services.notify import enqueue as enqueue_module
        from services.notify import worker as worker_module
        from services.notify.events import LIVE
        from services.notify.flags import load_flags
        from services.notify.providers import google_event_id
        from services.notify.security import encrypt_secret

        key = Fernet.generate_key().decode("ascii")
        flags = load_flags({
            "CPL_GOOGLE_CALENDAR_ENABLED": "true",
            "CPL_GOOGLE_CALENDAR_CLIENT_ID": "client-id",
            "CPL_GOOGLE_CALENDAR_CLIENT_SECRET": "client-secret",
            "CPL_OAUTH_REDIRECT_BASE": "https://api.example",
            "CPL_CALENDAR_TOKEN_KEY": key,
            "CPL_CALENDAR_LIVE_UPDATES_ENABLED": "true",
        })
        events: dict[str, dict] = {}
        calls: list[tuple[str, str]] = []

        class Response:
            def __init__(self, status_code, body=None):
                self.status_code = status_code
                self._body = body or {}

            def json(self):
                return self._body

        def post(url, data=None, headers=None):
            return Response(200, {"access_token": "access-token-for-test"})

        def request(method, url, json=None, headers=None):
            event_id = json["id"] if method == "POST" else url.rsplit("/", 1)[-1]
            calls.append((method, event_id))
            if method == "PUT":
                if event_id not in events:
                    return Response(404)
                events[event_id] = json
                return Response(200, {"id": event_id})
            events[event_id] = json
            return Response(200, {"id": event_id})

        def deliver_pending() -> int:
            cursor = self.db.cursor(dictionary=True)
            cursor.execute(
                "SELECT Id, MatchId, EventType, Payload, Attempts FROM dbo.tblNotificationOutbox "
                "WHERE MatchId = %s AND Status = 'pending' ORDER BY Id",
                (self.match_id,),
            )
            rows = cursor.fetchall()
            for row in rows:
                worker_module.deliver_item(self.repo, row, flags, post, request)
            return len(rows)

        def publish(kind: str, before, after) -> None:
            with mock.patch.object(enqueue_module, "load_flags", return_value=flags):
                enqueue_module.publish_match_change(self.db, kind, before, after)

        def set_match(**columns) -> dict:
            before = self.repo.match_notice(self.match_id)
            assignments = ", ".join(f"{name} = %s" for name in columns)
            cursor = self.db.cursor()
            cursor.execute(
                f"UPDATE dbo.tblMatches SET {assignments}, UpdatedAt = SYSUTCDATETIME() WHERE Id = %s",
                (*columns.values(), self.match_id),
            )
            self.db.commit()
            return before

        admin_id = next(_counter)
        connection_id = self.repo.save_connection(admin_id, "google", encrypt_secret(key, "refresh-for-test"), "primary")
        google_id = google_event_id(self.match_id)
        try:
            # Other tests leave connections behind in the scratch database. This test checks
            # only its own calendar, so the others are voided for the run.
            cursor = self.db.cursor()
            cursor.execute("UPDATE dbo.tblCalendarConnections SET Void = 1 WHERE Void = 0 AND Id <> %s", (connection_id,))
            self.db.commit()

            # Scenario B: the match is created. One event is written.
            publish("created", None, self.repo.match_notice(self.match_id))
            self.assertEqual(deliver_pending(), 1)
            self.assertEqual(calls, [("PUT", google_id), ("POST", google_id)])
            self.assertEqual(self.repo.event_map(connection_id, self.match_id)["external_event_id"], google_id)

            # Scenario C: the date moves. The existing event is updated in place, not duplicated.
            before = set_match(ScheduledDate=date(2026, 11, 21))
            publish("updated", before, self.repo.match_notice(self.match_id))
            self.assertEqual(deliver_pending(), 1)
            self.assertEqual(calls[-1], ("PUT", google_id))
            self.assertEqual(len(events), 1)
            self.assertEqual(events[google_id]["start"]["date"], "2026-11-21")

            # Scenario D: the match goes live. The calendar shows it, on the same event.
            before = set_match(Status=2)
            publish("updated", before, self.repo.match_notice(self.match_id))
            self.assertEqual(deliver_pending(), 1)
            self.assertTrue(events[google_id]["summary"].startswith("Live:"), events[google_id]["summary"])
            live_snapshot = self.repo.match_notice(self.match_id)

            # Scenario E: the match ends with a result. The event carries it.
            before = set_match(Status=3, Outcome=1, NetPoints=5)
            publish("updated", before, self.repo.match_notice(self.match_id))
            self.assertEqual(deliver_pending(), 1)
            self.assertIn("won by 5", events[google_id]["summary"])
            self.assertEqual(len(events), 1)

            # An older live notice, queued before the result, is delivered now. It must not put
            # the finished match back to live: the worker writes the match's current state.
            stale = dict(live_snapshot, status=2)
            self.repo.enqueue(stale, LIVE, key=f"{self.match_id}:stale-live:{next(_counter)}")
            self.assertEqual(deliver_pending(), 1)
            self.assertIn("won by 5", events[google_id]["summary"])
            self.assertFalse(events[google_id]["summary"].startswith("Live:"))

            # The match is cancelled. The event is marked cancelled, not removed.
            before = set_match(Void=1)
            publish("updated", before, self.repo.match_notice(self.match_id))
            self.assertEqual(deliver_pending(), 1)
            self.assertEqual(events[google_id]["status"], "cancelled")
            self.assertEqual([method for method, _ in calls].count("POST"), 1)
            self.assertEqual({event for _, event in calls}, {google_id})
        finally:
            cursor = self.db.cursor()
            cursor.execute("UPDATE dbo.tblCalendarConnections SET Void = 1 WHERE Id = %s", (connection_id,))
            self.db.commit()

    def _outbox_row(self, outbox_id: int) -> dict:
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT Status, Attempts, LastError FROM dbo.tblNotificationOutbox WHERE Id = %s",
            (outbox_id,),
        )
        return cursor.fetchone()

    def _enqueue(self, event: str = "scheduled") -> int:
        from tests.test_notify import _match

        match = _match(id=self.match_id)
        key = f"{self.match_id}:{event}:{next(_counter)}"
        outbox_id = self.repo.enqueue(match, event, key=key)
        self.assertIsNotNone(outbox_id)
        return outbox_id

    def _claimed_ids(self) -> list[int]:
        return [int(row["Id"]) for row in self.repo.claim_pending() if int(row["MatchId"]) == self.match_id]

    def test_a_claimed_row_is_leased_and_not_handed_out_twice(self):
        outbox_id = self._enqueue()
        self.assertEqual(self._claimed_ids(), [outbox_id])
        self.assertEqual(self._outbox_row(outbox_id)["Status"], "leased")
        self.assertEqual(self._claimed_ids(), [])

    def test_a_fresh_notice_is_claimable_straight_away(self):
        for _ in range(10):
            outbox_id = self._enqueue()
            self.assertEqual(self._claimed_ids(), [outbox_id])

    def test_a_lease_that_expired_is_reclaimed_with_one_more_attempt(self):
        outbox_id = self._enqueue()
        self._claimed_ids()
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE dbo.tblNotificationOutbox SET AvailableAt = DATEADD(MINUTE, -1, SYSUTCDATETIME()) WHERE Id = %s",
            (outbox_id,),
        )
        self.db.commit()
        self.assertEqual(self._claimed_ids(), [outbox_id])
        self.assertEqual(self._outbox_row(outbox_id)["Attempts"], 2)

    def test_a_transient_failure_waits_for_the_providers_retry_after(self):
        outbox_id = self._enqueue()
        self._claimed_ids()
        self.repo.mark_retry(outbox_id, 1, "rate_limited", 600)
        self.assertEqual(self._outbox_row(outbox_id)["Status"], "pending")
        self.assertEqual(self._claimed_ids(), [])
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT DATEDIFF(SECOND, SYSUTCDATETIME(), AvailableAt) AS Wait FROM dbo.tblNotificationOutbox WHERE Id = %s",
            (outbox_id,),
        )
        self.assertGreater(int(cursor.fetchone()["Wait"]), 500)

    def test_the_eighth_failed_attempt_leaves_the_row_dead_with_its_reason(self):
        outbox_id = self._enqueue()
        cursor = self.db.cursor()
        cursor.execute("UPDATE dbo.tblNotificationOutbox SET Attempts = 7 WHERE Id = %s", (outbox_id,))
        self.db.commit()
        self._claimed_ids()
        self.repo.mark_retry(outbox_id, 8, "provider_unavailable")
        row = self._outbox_row(outbox_id)
        self.assertEqual(row["Status"], "dead")
        self.assertEqual(row["LastError"], "provider_unavailable")
        self.assertNotIn(outbox_id, self._claimed_ids())

    def test_a_revoked_connection_stops_receiving_writes_and_is_listed_for_reauthorization(self):
        admin_id = next(_counter)
        self.repo.save_connection(admin_id, "google", "encrypted-token", "primary")
        connection_id = self.repo.connections_for_admin(admin_id)[0]["id"]
        self.assertIn(connection_id, [c["id"] for c in self.repo.active_connections()])
        self.repo.set_connection_status(connection_id, "revoked")
        self.assertNotIn(connection_id, [c["id"] for c in self.repo.active_connections()])
        self.assertEqual([row["status"] for row in self.repo.connections_for_admin(admin_id)], ["revoked"])
        self._void_connections(admin_id)

    def test_disconnect_works_only_for_the_owner_and_blanks_the_token(self):
        owner, other = next(_counter), next(_counter)
        self.repo.save_connection(owner, "microsoft", "encrypted-token", "me")
        connection_id = self.repo.connections_for_admin(owner)[0]["id"]
        self.assertFalse(self.repo.disconnect_connection(connection_id, other))
        self.assertIsNone(self.repo.connection_for_admin(connection_id, other))
        self.assertTrue(self.repo.disconnect_connection(connection_id, owner))
        self.assertEqual(self.repo.connections_for_admin(owner), [])
        cursor = self.db.cursor()
        cursor.execute("SELECT Void, Status, RefreshToken FROM dbo.tblCalendarConnections WHERE Id = %s", (connection_id,))
        row = cursor.fetchone()
        self.assertEqual((bool(row["Void"]), row["Status"], row["RefreshToken"]), (True, "revoked", ""))

    def test_a_revoked_feed_token_no_longer_resolves(self):
        token = f"test-feed-{next(_counter)}-token"
        self.repo.create_feed(self.season_id, None, token, "Carrom Premier League")
        self.assertIsNotNone(self.repo.feed_by_token(token))
        self.assertTrue(self.repo.revoke_feed(token))
        self.assertIsNone(self.repo.feed_by_token(token))

    def test_a_season_feed_lists_only_that_seasons_matches(self):
        other_season = self._insert_season()
        other_match = self._insert_match(other_season)
        try:
            self.assertEqual([m["id"] for m in self.repo.feed_matches(self.season_id, None)], [self.match_id])
            self.assertEqual([m["id"] for m in self.repo.feed_matches(other_season, None)], [other_match])
        finally:
            cursor = self.db.cursor()
            cursor.execute("DELETE FROM dbo.tblMatches WHERE Id = %s", (other_match,))
            cursor.execute("DELETE FROM dbo.tblSeasons WHERE Id = %s", (other_season,))
            self.db.commit()

    def test_a_season_rename_touches_only_that_seasons_matches_and_names_them(self):
        from services.notify.events import CALENDAR

        other_season = self._insert_season()
        other_match = self._insert_match(other_season)
        try:
            before = self.repo.match_notice(self.match_id)["updated_at"]
            other_before = self.repo.match_notice(other_match)["updated_at"]
            time.sleep(0.05)  # DATETIME2(6): the touch must land after the first stamp

            notices = self.repo.touch_matches_for_rename(season_id=self.season_id)

            self.assertEqual([n["id"] for n in notices], [self.match_id])
            self.assertGreater(notices[0]["updated_at"], before)
            self.assertEqual(self.repo.match_notice(other_match)["updated_at"], other_before)
            self.assertEqual(self.repo.entity_name("season", self.season_id), notices[0]["season_name"])
            outbox_id = self.repo.enqueue(notices[0], CALENDAR, key=f"{self.match_id}:{CALENDAR}:rename:sql-{next(_counter)}")
            self.assertIsNotNone(outbox_id)
        finally:
            cursor = self.db.cursor()
            cursor.execute("DELETE FROM dbo.tblNotificationOutbox WHERE MatchId = %s", (other_match,))
            cursor.execute("DELETE FROM dbo.tblMatches WHERE Id = %s", (other_match,))
            cursor.execute("DELETE FROM dbo.tblSeasons WHERE Id = %s", (other_season,))
            self.db.commit()

    def test_a_team_rename_with_no_matches_touches_nothing(self):
        before = self.repo.match_notice(self.match_id)["updated_at"]
        self.assertEqual(self.repo.touch_matches_for_rename(team_id=2_000_000_000), [])
        self.assertEqual(self.repo.match_notice(self.match_id)["updated_at"], before)

    def test_a_withdrawn_match_is_skipped_until_it_is_added_back(self):
        admin_id = next(_counter)
        connection_id = self.repo.save_connection(admin_id, "google", "encrypted-token", "primary")
        self.assertEqual(self.repo.withdrawn_connection_ids(self.match_id), set())

        # Never written: withdrawing still records the choice, with no event to delete yet.
        self.repo.set_withdrawn(connection_id, self.match_id, True)
        self.assertEqual(self.repo.withdrawn_connection_ids(self.match_id), {connection_id})
        state = [s for s in self.repo.connection_match_states(admin_id, self.match_id) if s["id"] == connection_id][0]
        self.assertEqual((state["removed"], state["on_calendar"]), (True, False))

        # Written, then withdrawn: the event id survives so the worker can delete it.
        self.repo.set_withdrawn(connection_id, self.match_id, False)
        self.repo.save_event_map(connection_id, self.match_id, "cplmatch0000000001", "a" * 64)
        state = [s for s in self.repo.connection_match_states(admin_id, self.match_id) if s["id"] == connection_id][0]
        self.assertEqual((state["removed"], state["on_calendar"]), (False, True))
        self.repo.set_withdrawn(connection_id, self.match_id, True)
        self.assertEqual(self.repo.event_map(connection_id, self.match_id)["external_event_id"], "cplmatch0000000001")
        self.assertEqual(self.repo.event_map(connection_id, self.match_id)["content_hash"].strip(), "")

        # Added back: active again, and the cleared hash means the next write is not skipped.
        self.repo.set_withdrawn(connection_id, self.match_id, False)
        self.assertEqual(self.repo.withdrawn_connection_ids(self.match_id), set())
        state = [s for s in self.repo.connection_match_states(admin_id, self.match_id) if s["id"] == connection_id][0]
        self.assertEqual((state["removed"], state["on_calendar"]), (False, False))
        self.assertEqual(self.repo.event_map(connection_id, self.match_id)["content_hash"].strip(), "")

    def test_the_same_notice_key_is_queued_once(self):
        from tests.test_notify import _match

        key = f"{self.match_id}:live:dedupe-{next(_counter)}"
        first = self.repo.enqueue(_match(id=self.match_id), "live", key=key)
        second = self.repo.enqueue(_match(id=self.match_id), "live", key=key)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        cursor = self.db.cursor()
        cursor.execute("SELECT COUNT(*) AS Total FROM dbo.tblNotificationOutbox WHERE IdempotencyKey = %s", (key,))
        self.assertEqual(int(cursor.fetchone()["Total"]), 1)

    def _subscription_row(self, endpoint: str, **overrides) -> dict:
        row = {
            "endpoint": endpoint, "p256dh": "p256dh-key", "auth": "auth-secret", "season_id": self.season_id,
            "team_id": None, "admin_id": None, "user_agent": "test",
            "notify_scheduled": True, "notify_changed": True, "notify_cancelled": True, "notify_live": True,
            "notify_ended": True, "notify_score": True, "notify_reminder": True,
        }
        row.update(overrides)
        return row

    def test_a_device_resubscribing_reuses_its_row_and_can_unsubscribe(self):
        endpoint = f"https://push.example.test/{next(_counter)}"
        first = self.repo.save_subscription(self._subscription_row(endpoint))
        self.assertTrue(self.repo.void_subscription(endpoint))
        again = self.repo.save_subscription(self._subscription_row(endpoint, notify_score=False))
        self.assertEqual(again["id"], first["id"])
        self.assertFalse(again["void"])
        self.assertFalse(again["notify_score"])
        updated = self.repo.update_preferences(endpoint, {"notify_live": False})
        self.assertEqual((updated["notify_live"], updated["notify_score"]), (False, False))
        self.assertIn(first["id"], [s["id"] for s in self.repo.active_subscriptions()])
        self.assertTrue(self.repo.void_subscription(endpoint))
        self.assertNotIn(first["id"], [s["id"] for s in self.repo.active_subscriptions()])
        self.assertIsNone(self.repo.update_preferences(endpoint, {"notify_live": True}))

    def test_a_gone_endpoint_is_voided_at_once_and_soft_failures_after_eight(self):
        gone = self.repo.save_subscription(self._subscription_row(f"https://push.example.test/{next(_counter)}"))
        self.repo.note_push_failure(gone["id"], gone=True)
        self.assertTrue(self.repo.subscription_by_endpoint(gone["endpoint"])["void"])
        flaky = self.repo.save_subscription(self._subscription_row(f"https://push.example.test/{next(_counter)}"))
        for _ in range(7):
            self.repo.note_push_failure(flaky["id"], gone=False)
        self.assertFalse(self.repo.subscription_by_endpoint(flaky["endpoint"])["void"])
        self.repo.note_push_failure(flaky["id"], gone=False)
        self.assertTrue(self.repo.subscription_by_endpoint(flaky["endpoint"])["void"])

    def test_a_push_success_clears_failures_so_only_consecutive_ones_void_a_device(self):
        device = self.repo.save_subscription(self._subscription_row(f"https://push.example.test/{next(_counter)}"))
        for _ in range(7):
            self.repo.note_push_failure(device["id"], gone=False)
        self.repo.note_push_success(device["id"])
        row = self.repo.subscription_by_endpoint(device["endpoint"])
        self.assertEqual(row["failure_count"], 0)
        self.assertIsNotNone(row["last_success_at"])
        for _ in range(7):
            self.repo.note_push_failure(device["id"], gone=False)
        self.assertFalse(self.repo.subscription_by_endpoint(device["endpoint"])["void"])

    def test_the_sync_problem_choice_is_saved_and_a_notice_is_queued_once_per_connection(self):
        admin_id = next(_counter)
        device = self.repo.save_subscription(self._subscription_row(
            f"https://push.example.test/{next(_counter)}", admin_id=admin_id, notify_sync_problem=False))
        self.assertFalse(device["notify_sync_problem"])
        self.assertTrue(self.repo.update_preferences(device["endpoint"], {"notify_sync_problem": True})["notify_sync_problem"])
        self.repo.save_connection(admin_id, "google", "encrypted-token", "primary")
        connection_id = self.repo.connections_for_admin(admin_id)[0]["id"]
        self.assertIsNotNone(self.repo.enqueue_sync_problem(connection_id, admin_id, "google"))
        self.assertIsNone(self.repo.enqueue_sync_problem(connection_id, admin_id, "google"))
        key = f"connection:{connection_id}:sync_problem"
        cursor = self.db.cursor(dictionary=True)
        cursor.execute("SELECT COUNT(*) AS Total FROM dbo.tblNotificationOutbox WHERE IdempotencyKey = %s", (key,))
        self.assertEqual(int(cursor.fetchone()["Total"]), 1)
        # This row has match id 0, so the shared teardown does not remove it.
        cursor.execute("DELETE FROM dbo.tblNotificationOutbox WHERE IdempotencyKey = %s", (key,))
        self.db.commit()

    def test_reconnecting_keeps_the_written_events_and_the_removed_matches(self):
        admin_id = next(_counter)
        first = self.repo.save_connection(admin_id, "microsoft", "encrypted-token", "me")
        self.repo.save_event_map(first, self.match_id, "graph-event-1", "a" * 64)

        second = self.repo.save_connection(admin_id, "microsoft", "encrypted-token-2", "me")
        self.assertNotEqual(first, second)
        # The mapped id carries over with no hash, so the backfill PATCHes it instead of posting a copy.
        self.assertEqual(self.repo.event_map(second, self.match_id), {"external_event_id": "graph-event-1", "content_hash": ""})
        self.assertNotIn(second, self.repo.withdrawn_connection_ids(self.match_id))

        self.repo.set_withdrawn(second, self.match_id, True)
        third = self.repo.save_connection(admin_id, "microsoft", "encrypted-token-3", "me")
        self.assertIn(third, self.repo.withdrawn_connection_ids(self.match_id))
        self.assertEqual([c["id"] for c in self.repo.connections_for_admin(admin_id)], [third])

    def test_a_calendar_event_map_is_updated_in_place(self):
        admin_id = next(_counter)
        self.repo.save_connection(admin_id, "google", "encrypted-token", "primary")
        connection_id = self.repo.connections_for_admin(admin_id)[0]["id"]
        self.assertIsNone(self.repo.event_map(connection_id, self.match_id))
        self.repo.save_event_map(connection_id, self.match_id, "cplmatch0000000001", "a" * 64)
        self.repo.save_event_map(connection_id, self.match_id, "cplmatch0000000001", "b" * 64)
        self.assertEqual(
            self.repo.event_map(connection_id, self.match_id),
            {"external_event_id": "cplmatch0000000001", "content_hash": "b" * 64},
        )
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT COUNT(*) AS Total FROM dbo.tblCalendarEventMaps WHERE ConnectionId = %s AND MatchId = %s",
            (connection_id, self.match_id),
        )
        self.assertEqual(int(cursor.fetchone()["Total"]), 1)
        self._void_connections(admin_id)

    def test_an_endpoint_longer_than_the_index_key_limit_saves(self):
        endpoint = "https://push.example.test/" + "x" * 1950 + str(next(_counter))
        saved = self.repo.save_subscription(self._subscription_row(endpoint))
        self.assertEqual(saved["endpoint"], endpoint)
        self.assertEqual(self.repo.subscription_by_endpoint(endpoint)["id"], saved["id"])

    def test_a_new_calendar_is_filled_with_upcoming_matches_once(self):
        from services.notify.enqueue import backfill_connection

        admin_id = next(_counter)
        connection_id = self.repo.save_connection(admin_id, "google", "encrypted-token", "primary")
        self.assertEqual(connection_id, self.repo.connections_for_admin(admin_id)[0]["id"])
        self.assertIn(self.match_id, [m["id"] for m in self.repo.upcoming_matches(date(2026, 11, 1))])
        self.assertNotIn(self.match_id, [m["id"] for m in self.repo.upcoming_matches(date(2026, 12, 1))])
        first = backfill_connection(self.db, connection_id, date(2026, 11, 1))
        again = backfill_connection(self.db, connection_id, date(2026, 11, 1))
        self.assertGreaterEqual(first, 1)
        self.assertEqual(again, 0)
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT Payload FROM dbo.tblNotificationOutbox WHERE IdempotencyKey = %s",
            (f"{self.match_id}:sync:{connection_id}",),
        )
        self.assertIn(f'"connection_id":{connection_id}', cursor.fetchone()["Payload"])
        self._void_connections(admin_id)

    def test_sent_targets_and_sync_state_come_from_the_delivery_log(self):
        admin_id = next(_counter)
        connection_id = self.repo.save_connection(admin_id, "google", "encrypted-token", "primary")
        outbox_id = self._enqueue()
        self.repo.record_delivery(outbox_id, "push", "41", "sent", "")
        self.repo.record_delivery(outbox_id, "push", "42", "failed", "rate_limited")
        self.repo.record_delivery(outbox_id, "google", str(connection_id), "sent", "")
        self.repo.record_delivery(outbox_id, "google", str(connection_id), "failed", "provider_unavailable")
        self.assertEqual(self.repo.sent_targets(outbox_id, "push"), {"41"})
        state = self.repo.connection_sync_states([connection_id])[connection_id]
        self.assertEqual((state["last_status"], state["last_detail"]), ("failed", "provider_unavailable"))
        self.assertTrue(state["queue_pending"])
        health = self.repo.delivery_health()
        self.assertGreaterEqual(health["backlog"], 1)
        self.assertGreaterEqual(health["failed_last_24h"], 2)
        self.assertIsNotNone(health["oldest_pending_seconds"])
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM dbo.tblNotificationDeliveries WHERE OutboxId = %s", (outbox_id,))
        self.db.commit()
        self._void_connections(admin_id)

    def test_a_schedule_rebuild_deletes_a_synced_match_and_queues_its_withdrawal(self):
        from unittest import mock

        from repositories.match_repository import MatchRepository
        from services.notify import enqueue as enqueue_module
        from tests.test_notify import _ready_flags

        admin_id = next(_counter)
        connection_id = self.repo.save_connection(admin_id, "google", "encrypted-token", "primary")
        self.repo.save_event_map(connection_id, self.match_id, "cplmatch0000000001", "a" * 64)
        with mock.patch.object(enqueue_module, "load_flags", return_value=_ready_flags()):
            removed = enqueue_module.season_scheduled(self.db, self.season_id)
            self.assertEqual([m["id"] for m in removed], [self.match_id])
            # The event map has no foreign key to the match, so the rebuild's delete succeeds.
            self.assertEqual(MatchRepository(self.db).replace_unplayed_matches(self.season_id, [], date(2026, 11, 20)), (1, 0))
            self.assertEqual(enqueue_module.publish_schedule_rebuild(self.db, self.season_id, removed), 1)
        self.assertIsNone(self.repo.match_notice(self.match_id))
        self.assertIsNotNone(self.repo.event_map(connection_id, self.match_id))
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT EventType, Payload FROM dbo.tblNotificationOutbox WHERE IdempotencyKey = %s",
            (f"{self.match_id}:calendar:removed",),
        )
        row = cursor.fetchone()
        self.assertEqual(row["EventType"], "calendar")
        self.assertIn('"void":true', row["Payload"])
        self._void_connections(admin_id)

    def test_a_feed_token_is_stored_only_as_its_hash(self):
        from repositories.notify_repository import token_hash

        token = f"test-feed-{next(_counter)}-plain"
        self.repo.create_feed(self.season_id, None, token, "Carrom Premier League")
        cursor = self.db.cursor()
        cursor.execute("SELECT Token FROM dbo.tblCalendarFeeds WHERE SeasonId = %s", (self.season_id,))
        self.assertEqual([row["Token"] for row in cursor.fetchall()], [token_hash(token)])
        self.assertIsNotNone(self.repo.feed_by_token(token))
        self.assertIsNone(self.repo.feed_by_token(token_hash(token)))

    def _void_connections(self, admin_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute("UPDATE dbo.tblCalendarConnections SET Void = 1 WHERE AdminId = %s", (admin_id,))
        self.db.commit()


if __name__ == "__main__":
    unittest.main()
