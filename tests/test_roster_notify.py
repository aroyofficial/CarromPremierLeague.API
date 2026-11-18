"""Player names in calendar events: the description, the outbox snapshot, and the rename and roster triggers.

The SQL class runs only when CPL_NOTIFY_SQL_TEST_CONNECTION holds a connection string for a
disposable database, the same rule as tests/test_notify_sql.py.
"""

from __future__ import annotations

import time
import unittest
from datetime import date

from tests import test_auth_service as _env  # noqa: F401  (sets required app env before config imports)

from tests.test_notify import _match, _ready_flags
from tests.test_notify_sql import CONNECTION_STRING, _connect, _counter


class RosterCopyTests(unittest.TestCase):
    def test_the_description_lists_each_teams_players(self):
        from services.notify.copy import description

        match = _match(team1_players=["Ada Lovelace", "Cy Twombly"], team2_players=["Bo Ray"])
        text = description(match, "https://web.example")
        self.assertIn("Red players: Ada Lovelace, Cy Twombly", text)
        self.assertIn("Blue players: Bo Ray", text)

    def test_no_player_line_when_no_roster_is_listed(self):
        from services.notify.copy import description

        self.assertNotIn("players:", description(_match(team1_players=[], team2_players=[]), ""))
        self.assertNotIn("players:", description(_match(), ""))

    def test_a_team_without_a_name_gets_a_generic_label(self):
        from services.notify.copy import roster_lines

        self.assertEqual(roster_lines(_match(team1_name="", team1_players=["X Y"], team2_players=[])), ["Team players: X Y"])

    def test_the_outbox_snapshot_leaves_out_roster_names(self):
        from repositories.notify_repository import snapshot

        snap = snapshot(_match(team1_players=["Ada Lovelace"], team2_players=[]))
        self.assertNotIn("team1_players", snap)
        self.assertNotIn("team2_players", snap)
        self.assertEqual(snap["team1_name"], "Red")


class PlayerChangeQueueTests(unittest.TestCase):
    """publish_player_change queues one calendar-only write per match the player is in."""

    def _fake_repository(self, matches, calls):
        class Repository:
            def __init__(self, db):
                pass

            def touch_matches_for_player(self, player_id, season_id=None):
                calls.append(("touch", player_id, season_id))
                return matches

            def enqueue(self, match, event, before=None, key=None, connection_id=None, existing_only=False):
                calls.append(("enqueue", int(match["id"]), event, key, existing_only))
                return len(calls)

        return Repository

    def test_each_match_gets_one_calendar_only_write(self):
        from unittest import mock

        from services.notify import enqueue as enqueue_module
        from services.notify.events import CALENDAR

        calls = []
        matches = [_match(id=7), _match(id=8)]
        with mock.patch.object(enqueue_module, "NotifyRepository", self._fake_repository(matches, calls)), \
                mock.patch.object(enqueue_module, "load_flags", return_value=_ready_flags()):
            queued = enqueue_module.publish_player_change(None, player_id=5, season_id=3)
        self.assertEqual(queued, 2)
        self.assertEqual(calls[0], ("touch", 5, 3))
        enqueued = [call for call in calls if call[0] == "enqueue"]
        self.assertEqual([call[1] for call in enqueued], [7, 8])
        self.assertTrue(all(call[2] == CALENDAR for call in enqueued))
        self.assertTrue(all(":roster:" in call[3] for call in enqueued))
        self.assertTrue(all(call[4] is True for call in enqueued))

    def test_flags_off_touches_nothing(self):
        from unittest import mock

        from services.notify import enqueue as enqueue_module

        off = _ready_flags(push_enabled=False, google_enabled=False, microsoft_enabled=False)
        with mock.patch.object(enqueue_module, "load_flags", return_value=off), \
                mock.patch.object(enqueue_module, "NotifyRepository", side_effect=AssertionError("no repository")):
            self.assertEqual(enqueue_module.publish_player_change(None, player_id=5), 0)

    def test_a_failing_queue_does_not_raise(self):
        from unittest import mock

        from services.notify import enqueue as enqueue_module

        class Broken:
            def __init__(self, db):
                pass

            def touch_matches_for_player(self, player_id, season_id=None):
                raise RuntimeError("outbox unavailable")

        with mock.patch.object(enqueue_module, "NotifyRepository", Broken), \
                mock.patch.object(enqueue_module, "load_flags", return_value=_ready_flags()):
            self.assertEqual(enqueue_module.publish_player_change(None, player_id=5), 0)


class PlayerRouteHookTests(unittest.TestCase):
    """The routes that change a name or a roster call the trigger after the write."""

    def test_a_name_change_triggers_the_player_queue(self):
        from unittest import mock

        from routes import player_routes
        from schemas.player_schema import PlayerUpdateRequest

        class Controller:
            def update(self, player_id, request):
                return "updated"

        with mock.patch.object(player_routes, "publish_player_change") as publish:
            player_routes.update(7, PlayerUpdateRequest(first_name="Ada"), controller=Controller(), db="DB")
        publish.assert_called_once_with("DB", player_id=7)

    def test_a_change_that_is_not_a_name_does_not_trigger_it(self):
        from unittest import mock

        from routes import player_routes
        from schemas.player_schema import PlayerUpdateRequest

        class Controller:
            def update(self, player_id, request):
                return "updated"

        with mock.patch.object(player_routes, "publish_player_change") as publish:
            player_routes.update(7, PlayerUpdateRequest(nationality_id=2), controller=Controller(), db="DB")
        publish.assert_not_called()

    def test_assigning_and_removing_a_roster_row_both_trigger_it(self):
        from unittest import mock

        from routes import roster_routes
        from schemas.roster_schema import RemovePlayerRequest, RosterAssignRequest

        class Controller:
            def assign_player(self, season_id, team_id, player_id):
                return "assigned"

            def remove_player(self, season_id, player_id):
                return True

        with mock.patch.object(roster_routes, "publish_player_change") as publish:
            roster_routes.assign_player(
                RosterAssignRequest(season_id=3, team_id=1, player_id=5), controller=Controller(), db="DB"
            )
            roster_routes.remove_player(RemovePlayerRequest(season_id=3, player_id=5), controller=Controller(), db="DB")
        self.assertEqual(
            publish.call_args_list,
            [mock.call("DB", player_id=5, season_id=3), mock.call("DB", player_id=5, season_id=3)],
        )


@unittest.skipUnless(CONNECTION_STRING, "CPL_NOTIFY_SQL_TEST_CONNECTION is not set")
class RosterSqlTests(unittest.TestCase):
    """Rosters, renames, and removals against a real database."""

    def setUp(self):
        from repositories.notify_repository import NotifyRepository

        self.db = _connect()
        self.repo = NotifyRepository(self.db)
        tag = next(_counter)
        cursor = self.db.cursor()
        cursor.execute("INSERT INTO dbo.tblSeasons (Name, [Show]) VALUES (%s, 0)", (f"roster-test-{tag}",))
        self.season_id = int(cursor.lastrowid)
        self.team_a = self._team(f"Roster A {tag}")
        self.team_b = self._team(f"Roster B {tag}")
        cursor.execute(
            """
            INSERT INTO dbo.tblMatches (ScheduledDate, Category, [Order], SeasonId, Status, Team1, Team2)
            VALUES (%s, 1, %s, %s, 1, %s, %s)
            """,
            (date(2026, 11, 20), tag, self.season_id, self.team_a, self.team_b),
        )
        self.match_id = int(cursor.lastrowid)
        self.db.commit()
        self.player_a = self._player("Ada", "Lovelace")
        self.player_b = self._player("Bo", "Ray")
        self._roster(self.player_a, self.team_a)
        self._roster(self.player_b, self.team_b)

    def tearDown(self):
        cursor = self.db.cursor()
        cursor.execute(
            "DELETE FROM dbo.tblNotificationDeliveries WHERE OutboxId IN (SELECT Id FROM dbo.tblNotificationOutbox WHERE MatchId = %s)",
            (self.match_id,),
        )
        cursor.execute("DELETE FROM dbo.tblNotificationOutbox WHERE MatchId = %s", (self.match_id,))
        cursor.execute("DELETE FROM dbo.tblMatches WHERE Id = %s", (self.match_id,))
        cursor.execute("DELETE FROM dbo.tblPlayersSeasonsTeams WHERE SeasonId = %s", (self.season_id,))
        cursor.execute("DELETE FROM dbo.tblPlayers WHERE Id IN (%s, %s)", (self.player_a, self.player_b))
        cursor.execute("DELETE FROM dbo.tblSeasons WHERE Id = %s", (self.season_id,))
        cursor.execute("DELETE FROM dbo.tblTeams WHERE Id IN (%s, %s)", (self.team_a, self.team_b))
        self.db.commit()
        self.db.close()

    def _team(self, name: str) -> int:
        cursor = self.db.cursor()
        cursor.execute("INSERT INTO dbo.tblTeams (Name) VALUES (%s)", (name,))
        team_id = int(cursor.lastrowid)
        self.db.commit()
        return team_id

    def _player(self, first: str, last: str) -> int:
        cursor = self.db.cursor()
        cursor.execute("INSERT INTO dbo.tblPlayers (FirstName, LastName) VALUES (%s, %s)", (first, last))
        player_id = int(cursor.lastrowid)
        self.db.commit()
        return player_id

    def _roster(self, player_id: int, team_id: int) -> None:
        cursor = self.db.cursor()
        cursor.execute(
            "INSERT INTO dbo.tblPlayersSeasonsTeams (PlayerId, SeasonId, TeamId) VALUES (%s, %s, %s)",
            (player_id, self.season_id, team_id),
        )
        self.db.commit()

    def test_the_notice_lists_each_teams_roster(self):
        notice = self.repo.match_notice(self.match_id)
        self.assertEqual(notice["team1_players"], ["Ada Lovelace"])
        self.assertEqual(notice["team2_players"], ["Bo Ray"])

    def test_a_removed_roster_row_drops_the_name_and_still_touches_the_match(self):
        cursor = self.db.cursor()
        cursor.execute(
            "UPDATE dbo.tblPlayersSeasonsTeams SET Void = 1 WHERE PlayerId = %s AND SeasonId = %s",
            (self.player_a, self.season_id),
        )
        self.db.commit()
        self.assertEqual(self.repo.match_notice(self.match_id)["team1_players"], [])
        touched = self.repo.touch_matches_for_player(self.player_a, season_id=self.season_id)
        self.assertEqual([notice["id"] for notice in touched], [self.match_id])

    def test_a_rename_touches_the_match_and_the_next_read_has_the_new_name(self):
        before = self.repo.match_notice(self.match_id)
        cursor = self.db.cursor()
        cursor.execute("UPDATE dbo.tblPlayers SET FirstName = 'Augusta' WHERE Id = %s", (self.player_a,))
        self.db.commit()
        time.sleep(0.05)  # DATETIME2(6): the touch must land after the first stamp, as in test_notify_sql
        touched = self.repo.touch_matches_for_player(self.player_a)
        self.assertEqual([notice["id"] for notice in touched], [self.match_id])
        after = self.repo.match_notice(self.match_id)
        self.assertEqual(after["team1_players"], ["Augusta Lovelace"])
        self.assertGreater(after["updated_at"], before["updated_at"])

    def test_a_player_in_no_match_touches_nothing(self):
        stranger = self._player("No", "Match")
        try:
            self.assertEqual(self.repo.touch_matches_for_player(stranger), [])
        finally:
            cursor = self.db.cursor()
            cursor.execute("DELETE FROM dbo.tblPlayers WHERE Id = %s", (stranger,))
            self.db.commit()

    def test_the_feed_rows_carry_the_roster_names(self):
        rows = self.repo.feed_matches(self.season_id, None)
        self.assertEqual([row["id"] for row in rows], [self.match_id])
        self.assertEqual(rows[0]["team1_players"], ["Ada Lovelace"])
        self.assertEqual(rows[0]["team2_players"], ["Bo Ray"])


if __name__ == "__main__":
    unittest.main()
