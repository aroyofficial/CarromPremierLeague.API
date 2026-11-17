import unittest

from fastapi import HTTPException

from schemas.roster_schema import RosterResponse, TeamRosterPlayer, TeamRosterResponse
from services.roster_service import RosterService


def player(player_id):
    return TeamRosterPlayer(player_id=player_id, first_name="A", last_name="B", avatar_url=None)


class Repository:
    def __init__(self, players):
        self.players = players
        self.assigned = None

    def get_team_roster(self, season_id, team_id):
        return TeamRosterResponse(team_id=team_id, season_id=season_id, players=self.players)

    def assign_player(self, season_id, team_id, player_id):
        self.assigned = (season_id, team_id, player_id)
        return RosterResponse(id=1, player_id=player_id, season_id=season_id, team_id=team_id)


class TestRosterLimit(unittest.TestCase):
    def test_a_third_player_is_refused(self):
        repository = Repository([player(1), player(2)])
        with self.assertRaises(HTTPException) as caught:
            RosterService(repository).assign_player(1, 2, 3)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIsNone(repository.assigned)

    def test_a_player_already_on_a_full_team_can_stay(self):
        repository = Repository([player(1), player(2)])
        RosterService(repository).assign_player(1, 2, 2)
        self.assertEqual(repository.assigned, (1, 2, 2))

    def test_an_open_place_can_be_filled(self):
        repository = Repository([player(1)])
        RosterService(repository).assign_player(1, 2, 3)
        self.assertEqual(repository.assigned, (1, 2, 3))
