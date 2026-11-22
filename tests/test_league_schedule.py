import unittest
from datetime import date
from types import SimpleNamespace

from fastapi import HTTPException

from services.league_schedule import plan_league_schedule
from services.player_service import PlayerService
from services.team_service import TeamService


def match(match_id, team1, team2, status, category=1, order=1):
    return SimpleNamespace(
        id=match_id,
        team1=team1,
        team2=team2,
        status=status,
        category=category,
        order=order,
    )


def pairs_of(team_ids):
    teams = sorted(team_ids)
    return {frozenset((left, right)) for index, left in enumerate(teams) for right in teams[index + 1 :]}


def consecutive(pairings):
    return sum(1 for left, right in zip(pairings, pairings[1:]) if set(left) & set(right))


class TestPlan(unittest.TestCase):
    today = date(2026, 11, 18)
    upcoming = date(2026, 12, 27)

    def test_only_an_upcoming_season_can_be_scheduled(self):
        with self.assertRaises(ValueError) as started:
            plan_league_schedule([1, 2, 3, 4], [], date(2026, 11, 18), self.today)
        self.assertIn("upcoming", str(started.exception))
        with self.assertRaises(ValueError):
            plan_league_schedule([1, 2, 3, 4], [], None, self.today)

    def test_fewer_than_four_teams_is_rejected(self):
        with self.assertRaises(ValueError) as error:
            plan_league_schedule([1, 2, 3], [], self.upcoming, self.today)
        self.assertIn("four teams", str(error.exception))

    def test_six_teams_cover_every_pair_without_consecutive_matches(self):
        teams = [1, 2, 3, 4, 5, 6]
        pairings = plan_league_schedule(teams, [], self.upcoming, self.today)
        self.assertEqual({frozenset(pair) for pair in pairings}, pairs_of(teams))
        self.assertEqual(len(pairings), 15)
        self.assertEqual(consecutive(pairings), 0)

    def test_four_teams_cover_every_pair(self):
        teams = [1, 2, 3, 4]
        pairings = plan_league_schedule(teams, [], self.upcoming, self.today)
        self.assertEqual({frozenset(pair) for pair in pairings}, pairs_of(teams))
        self.assertEqual(len(pairings), 6)

    def test_started_league_matches_are_not_scheduled_again(self):
        teams = [1, 2, 3, 4, 5, 6]
        existing = [
            match(1, 1, 2, status=3, order=1),
            match(2, 3, 4, status=2, order=2),
            match(3, 5, 6, status=1, order=3),
        ]
        pairings = plan_league_schedule(teams, existing, self.upcoming, self.today)
        scheduled = {frozenset(pair) for pair in pairings}
        self.assertNotIn(frozenset((1, 2)), scheduled)
        self.assertNotIn(frozenset((3, 4)), scheduled)
        self.assertIn(frozenset((5, 6)), scheduled)
        self.assertEqual(scheduled, pairs_of(teams) - {frozenset((1, 2)), frozenset((3, 4))})
        self.assertTrue(set(pairings[0]).isdisjoint({3, 4}))

    def test_one_round_is_the_default(self):
        teams = [1, 2, 3, 4, 5]
        self.assertEqual(
            plan_league_schedule(teams, [], self.upcoming, self.today),
            plan_league_schedule(teams, [], self.upcoming, self.today, rounds=1),
        )

    def test_each_pair_plays_as_many_times_as_asked(self):
        teams = [1, 2, 3, 4, 5, 6]
        for rounds in (2, 3, 4):
            pairings = plan_league_schedule(teams, [], self.upcoming, self.today, rounds=rounds)
            counts = {}
            for pair in pairings:
                counts[frozenset(pair)] = counts.get(frozenset(pair), 0) + 1
            self.assertEqual(set(counts), pairs_of(teams))
            self.assertEqual(set(counts.values()), {rounds})
            self.assertEqual(len(pairings), 15 * rounds)
            self.assertEqual(consecutive(pairings), 0)

    def test_a_repeat_swaps_the_sides(self):
        pairings = plan_league_schedule([1, 2, 3, 4], [], self.upcoming, self.today, rounds=2)
        self.assertEqual(len(pairings), 12)
        sides = {}
        for home, away in pairings:
            sides.setdefault(frozenset((home, away)), []).append(home)
        self.assertTrue(all(len(set(homes)) == 2 for homes in sides.values()))

    def test_started_matches_count_towards_the_total_when_rescheduling(self):
        teams = [1, 2, 3, 4]
        existing = [
            match(1, 1, 2, status=3, order=1),
            match(2, 1, 2, status=2, order=2),
            match(3, 3, 4, status=3, order=3),
            match(4, 1, 3, status=1, order=4),
        ]
        pairings = plan_league_schedule(teams, existing, self.upcoming, self.today, rounds=2)
        remaining = {}
        for pair in pairings:
            remaining[frozenset(pair)] = remaining.get(frozenset(pair), 0) + 1
        self.assertNotIn(frozenset((1, 2)), remaining)
        self.assertEqual(remaining[frozenset((3, 4))], 1)
        self.assertEqual(remaining[frozenset((1, 3))], 2)
        self.assertEqual(len(pairings), 9)

    def test_fewer_rounds_than_already_played_adds_nothing_for_that_pair(self):
        existing = [match(1, 1, 2, status=3, order=1), match(2, 1, 2, status=3, order=2)]
        pairings = plan_league_schedule([1, 2, 3, 4], existing, self.upcoming, self.today, rounds=1)
        self.assertNotIn(frozenset((1, 2)), {frozenset(pair) for pair in pairings})

    def test_rounds_outside_the_range_are_rejected(self):
        for rounds in (0, -1, 5, 1.5, True, None):
            with self.assertRaises(ValueError) as error:
                plan_league_schedule([1, 2, 3, 4], [], self.upcoming, self.today, rounds=rounds)
            self.assertIn("times", str(error.exception))

    def test_a_played_knockout_does_not_remove_the_league_pairing(self):
        pairings = plan_league_schedule(
            [1, 2, 3, 4],
            [match(9, 1, 2, status=3, category=2, order=1)],
            self.upcoming,
            self.today,
        )
        self.assertIn(frozenset((1, 2)), {frozenset(pair) for pair in pairings})


class FakeTeams:
    def __init__(self, linked):
        self.linked = linked
        self.deleted = False

    def has_assignment(self, team_id):
        return self.linked

    def delete(self, team_id):
        self.deleted = True
        return True


class FakePlayers:
    def __init__(self, linked):
        self.linked = linked
        self.deleted = False

    def get_by_id(self, player_id):
        return SimpleNamespace(id=player_id)

    def has_roster(self, player_id):
        return self.linked

    def delete(self, player_id):
        self.deleted = True
        return True


class TestDeleteGuards(unittest.TestCase):
    def test_assigned_team_and_rostered_player_cannot_be_deleted(self):
        teams = FakeTeams(linked=True)
        with self.assertRaises(HTTPException) as team_error:
            TeamService(teams).delete(4)
        self.assertEqual(team_error.exception.status_code, 400)
        self.assertFalse(teams.deleted)

        players = FakePlayers(linked=True)
        with self.assertRaises(HTTPException) as player_error:
            PlayerService(players, None).delete(7)
        self.assertEqual(player_error.exception.status_code, 400)
        self.assertFalse(players.deleted)

    def test_unassigned_team_and_player_can_be_deleted(self):
        teams = FakeTeams(linked=False)
        self.assertTrue(TeamService(teams).delete(4))
        self.assertTrue(teams.deleted)

        players = FakePlayers(linked=False)
        self.assertTrue(PlayerService(players, None).delete(7))
        self.assertTrue(players.deleted)


if __name__ == "__main__":
    unittest.main()
