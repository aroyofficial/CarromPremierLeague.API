"""Worker fixes from the independent review: rename writes, late withdraw retries, and unreadable grants."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from tests import test_auth_service as _env  # noqa: F401  (sets required app env before config imports)

from tests.test_notify import _match, _ready_flags


class _Repo:
    def __init__(self, event_map=None, withdrawn=None):
        self._event_map = event_map
        self._withdrawn = withdrawn or set()
        self.deliveries = []
        self.statuses = []
        self.sync_problems = []
        self.done = []
        self.retries = []

    def event_map(self, connection_id, match_id):
        return self._event_map

    def withdrawn_connection_ids(self, match_id):
        return set(self._withdrawn)

    def record_delivery(self, outbox_id, channel, target, status, detail):
        self.deliveries.append((channel, status, detail))

    def save_event_map(self, *args):
        pass

    def set_connection_status(self, connection_id, status):
        self.statuses.append((connection_id, status))

    def enqueue_sync_problem(self, connection_id, admin_id, provider):
        self.sync_problems.append((connection_id, admin_id, provider))

    def match_notice(self, match_id):
        return _match(id=match_id)

    def active_subscriptions(self):
        return []

    def active_connections(self):
        return []

    def sent_targets(self, outbox_id, channel):
        return set()

    def mark_done(self, item_id):
        self.done.append(item_id)

    def mark_retry(self, *args, **kwargs):
        self.retries.append(args)


CONNECTION = {"id": 5, "admin_id": 2, "refresh_token": "stored"}
ITEM = {"Id": 4, "MatchId": 12, "EventType": "calendar", "Attempts": 1}


class RenameWriteTests(unittest.TestCase):
    def test_a_rename_skips_a_match_that_is_not_on_the_calendar(self):
        from services.notify import worker

        repo = _Repo(event_map=None)
        with mock.patch.object(worker, "refresh_access_token", side_effect=AssertionError("no provider call")), \
                mock.patch.object(worker, "upsert_google_event", side_effect=AssertionError("no provider call")):
            worker._deliver_google(repo, ITEM, _match(), CONNECTION, _ready_flags(), None, None, existing_only=True)
        self.assertEqual(repo.deliveries, [("google", "skipped", "not on this calendar")])

    def test_a_rename_rewrites_an_event_already_on_the_calendar(self):
        from services.notify import worker

        repo = _Repo(event_map={"external_event_id": "cplmatch0000000012", "content_hash": "old"})
        with mock.patch.object(worker, "decrypt_secret", return_value="refresh"), \
                mock.patch.object(worker, "refresh_access_token", return_value="access"), \
                mock.patch.object(worker, "upsert_google_event", return_value="cplmatch0000000012") as upsert:
            worker._deliver_google(repo, ITEM, _match(), CONNECTION, _ready_flags(), None, None, existing_only=True)
        upsert.assert_called_once()
        self.assertEqual(repo.deliveries, [("google", "sent", "")])

    def test_an_ordinary_write_still_creates_a_missing_event(self):
        from services.notify import worker

        repo = _Repo(event_map=None)
        with mock.patch.object(worker, "decrypt_secret", return_value="refresh"), \
                mock.patch.object(worker, "refresh_access_token", return_value="access"), \
                mock.patch.object(worker, "upsert_microsoft_event", return_value="graph-1") as upsert:
            worker._deliver_microsoft(repo, ITEM, _match(), CONNECTION, _ready_flags(), None, None)
        upsert.assert_called_once()


class LateWithdrawTests(unittest.TestCase):
    def test_a_withdraw_retry_after_the_match_was_added_back_deletes_nothing(self):
        from services.notify import worker

        repo = _Repo(event_map={"external_event_id": "cplmatch0000000012", "content_hash": "h"}, withdrawn=set())
        with mock.patch.object(worker, "delete_google_event", side_effect=AssertionError("must not delete")), \
                mock.patch.object(worker, "refresh_access_token", side_effect=AssertionError("no provider call")):
            worker._deliver_google(repo, ITEM, _match(), CONNECTION, _ready_flags(), None, None, withdraw=True)
        self.assertEqual(repo.deliveries, [("google", "skipped", "restored")])

    def test_a_withdraw_still_deletes_while_the_match_is_withdrawn(self):
        from services.notify import worker

        repo = _Repo(event_map={"external_event_id": "cplmatch0000000012", "content_hash": ""}, withdrawn={5})
        with mock.patch.object(worker, "decrypt_secret", return_value="refresh"), \
                mock.patch.object(worker, "refresh_access_token", return_value="access"), \
                mock.patch.object(worker, "delete_google_event") as delete:
            worker._deliver_google(repo, ITEM, _match(), CONNECTION, _ready_flags(), None, None, withdraw=True)
        delete.assert_called_once()
        self.assertEqual(repo.deliveries, [("google", "sent", "withdrawn")])


class UnreadableGrantTests(unittest.TestCase):
    def test_an_unreadable_grant_needs_reconnecting_and_is_not_retried(self):
        from services.notify import worker
        from services.notify.security import StateError

        repo = _Repo(event_map=None)
        item = dict(ITEM, EventType="scheduled", Payload=json.dumps({"event": "scheduled", "match": {"id": 12}}))
        action = {"channel": "google", "status": "send", "target": "5", "connection": CONNECTION}
        with mock.patch.object(worker, "delivery_plan", return_value=[action]), \
                mock.patch.object(worker, "decrypt_secret", side_effect=StateError("unreadable")):
            worker.deliver_item(repo, item, _ready_flags(), None, None)
        self.assertEqual(repo.statuses, [(5, "revoked")])
        self.assertEqual(repo.sync_problems, [(5, 2, "google")])
        self.assertEqual(repo.deliveries, [("google", "failed", "token_unreadable")])
        self.assertEqual((repo.done, repo.retries), ([4], []))


if __name__ == "__main__":
    unittest.main()
