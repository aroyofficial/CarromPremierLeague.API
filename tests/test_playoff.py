import unittest

from services.playoff import (
    knockout_fixtures,
    locked_seeds,
    qualification_tie_groups,
    seeds_after_ties,
    tie_breaker_groups,
)


def row(team_id, points=4, net=0, head_to_head=0, golden=0):
    return {
        "TeamId": team_id,
        "Points": points,
        "TotalNetPoints": net,
        "HeadToHeadWins": head_to_head,
        "GoldenStrikeWins": golden,
    }


class QualificationTieTests(unittest.TestCase):
    def test_separated_table_has_no_tie(self):
        rows = [row(1, points=6), row(2, points=4), row(3, points=2), row(4, points=0)]
        self.assertEqual(qualification_tie_groups(rows, 4), [])

    def test_head_to_head_splits_equal_points(self):
        rows = [row(1, points=4, head_to_head=1), row(2, points=4, head_to_head=0), row(3, points=2), row(4, points=0)]
        self.assertEqual(qualification_tie_groups(rows, 4), [])

    def test_four_teams_tie_for_first_schedules_both(self):
        rows = [row(1, points=4), row(2, points=4), row(3, points=2), row(4, points=0)]
        self.assertEqual(qualification_tie_groups(rows, 4), [[1, 2]])

    def test_four_teams_tie_for_second_includes_the_team_below(self):
        rows = [row(1, points=6), row(2, points=2), row(3, points=2), row(4, points=0)]
        self.assertEqual(qualification_tie_groups(rows, 4), [[2, 3]])

    def test_four_teams_ignore_a_tie_below_second(self):
        rows = [row(1, points=6), row(2, points=4), row(3, points=0), row(4, points=0)]
        self.assertEqual(qualification_tie_groups(rows, 4), [])

    def test_six_teams_tie_across_the_eliminator_cut(self):
        rows = [
            row(1, points=8),
            row(2, points=4),
            row(3, points=2),
            row(4, points=2),
            row(5, points=0),
            row(6, points=0),
        ]
        self.assertEqual(qualification_tie_groups(rows, 6), [[3, 4]])

    def test_six_teams_ignore_a_tie_below_the_eliminator(self):
        rows = [
            row(1, points=8),
            row(2, points=6),
            row(3, points=4),
            row(4, points=2),
            row(5, points=0),
            row(6, points=0),
        ]
        self.assertEqual(qualification_tie_groups(rows, 6), [])

    def test_three_teams_still_tied_after_golden_strike_stay_together(self):
        rows = [row(1, points=2, golden=1), row(2, points=2, golden=1), row(3, points=2, golden=1)]
        self.assertEqual(qualification_tie_groups(rows, 3), [[1, 2, 3]])


class KnockoutFixtureTests(unittest.TestCase):
    def test_two_teams_get_a_final(self):
        self.assertEqual(knockout_fixtures(2, [1, 2], None, False), [("final", 1, 2)])

    def test_four_teams_get_a_final_between_first_and_second(self):
        self.assertEqual(knockout_fixtures(4, [1, 2, 3, 4], None, False), [("final", 1, 2)])

    def test_unresolved_tie_keeps_the_final_as_tbd(self):
        self.assertEqual(knockout_fixtures(4, [1, 2, 3, 4], None, True), [("final", None, None)])

    def test_more_than_four_teams_send_first_straight_to_the_final(self):
        self.assertEqual(
            knockout_fixtures(6, [1, 2, 3, 4, 5, 6], None, False),
            [("eliminator", 2, 3), ("final", 1, None)],
        )

    def test_eliminator_winner_fills_both_final_teams(self):
        self.assertEqual(
            knockout_fixtures(6, [1, 2, 3, 4, 5, 6], 3, False),
            [("eliminator", 2, 3), ("final", 1, 3)],
        )

    def test_pending_tie_in_a_large_league_keeps_both_matches_tbd(self):
        self.assertEqual(
            knockout_fixtures(6, [1, 2, 3, 4, 5, 6], None, True),
            [("eliminator", None, None), ("final", None, None)],
        )

    def test_one_team_schedules_nothing(self):
        self.assertEqual(knockout_fixtures(1, [1], None, False), [])

    def test_unsettled_places_stay_tbd(self):
        self.assertEqual(knockout_fixtures(4, [1, None, 3, 4], None, False), [("final", 1, None)])
        self.assertEqual(
            knockout_fixtures(6, [None, 2, None, 4], None, False),
            [("eliminator", 2, None), ("final", None, None)],
        )

    def test_eliminator_result_moves_the_winner_into_the_final(self):
        self.assertEqual(
            knockout_fixtures(6, [1, 2, 3], None, False, {"eliminator": (3, 2)}),
            [("eliminator", 2, 3), ("final", 1, 3)],
        )


class FullBracketTests(unittest.TestCase):
    TEAMS = list(range(1, 9))

    def test_eight_teams_lay_out_the_whole_bracket(self):
        self.assertEqual(
            knockout_fixtures(8, self.TEAMS, None, False),
            [
                ("qualifier1", 1, 2),
                ("eliminator", 3, 4),
                ("qualifier2", None, None),
                ("final", None, None),
            ],
        )

    def test_pending_tie_keeps_every_match_tbd(self):
        self.assertEqual(
            knockout_fixtures(8, self.TEAMS, None, True),
            [
                ("qualifier1", None, None),
                ("eliminator", None, None),
                ("qualifier2", None, None),
                ("final", None, None),
            ],
        )

    def test_results_move_winners_and_losers_forward(self):
        results = {"qualifier1": (2, 1), "eliminator": (3, 4)}
        self.assertEqual(
            knockout_fixtures(8, self.TEAMS, None, False, results),
            [
                ("qualifier1", 1, 2),
                ("eliminator", 3, 4),
                ("qualifier2", 3, 1),
                ("final", 2, None),
            ],
        )

    def test_both_qualifier_winners_meet_in_the_final(self):
        results = {"qualifier1": (2, 1), "eliminator": (3, 4), "qualifier2": (1, 3)}
        self.assertEqual(knockout_fixtures(8, self.TEAMS, None, False, results)[-1], ("final", 2, 1))

    def test_eight_team_tie_boundaries_cover_the_four_qualifying_places(self):
        rows = [row(i, points=20 - 2 * i) for i in range(1, 9)]
        self.assertEqual(qualification_tie_groups(rows, 8), [])
        rows[3] = row(4, points=10)
        rows[4] = row(5, points=10)
        rows[2] = row(3, points=12)
        self.assertEqual(qualification_tie_groups(rows, 8), [[4, 5]])


class SeedTests(unittest.TestCase):
    def test_tied_places_are_dropped(self):
        self.assertEqual(seeds_after_ties([1, 2, 3, 4], [[2, 3]], 4), [1, None, None, 4])

    def test_clear_table_keeps_every_seed(self):
        self.assertEqual(seeds_after_ties([5, 6, 7, 8], [], 3), [5, 6, 7])

    def test_short_table_pads_with_none(self):
        self.assertEqual(seeds_after_ties([5], [], 3), [5, None, None])

    def test_leader_locks_when_nobody_can_catch_up(self):
        rows = [row(1, points=10), row(2, points=4), row(3, points=2)]
        remaining = {1: 0, 2: 1, 3: 1}
        self.assertEqual(locked_seeds(rows, remaining, 3), [1, None, None])

    def test_open_race_locks_nothing(self):
        rows = [row(1, points=4), row(2, points=4), row(3, points=2)]
        remaining = {1: 2, 2: 2, 3: 2}
        self.assertEqual(locked_seeds(rows, remaining, 3), [None, None, None])

    def test_finished_league_with_distinct_points_locks_all(self):
        rows = [row(1, points=6), row(2, points=4), row(3, points=2)]
        self.assertEqual(locked_seeds(rows, {}, 3), [1, 2, 3])

    def test_a_possible_tie_keeps_the_place_open(self):
        rows = [row(1, points=6), row(2, points=4), row(3, points=2)]
        self.assertEqual(locked_seeds(rows, {2: 1, 3: 2}, 3), [None, None, None])


def strike(match_id, team1, team2, status=3, winner=None):
    return {"match_id": match_id, "team1": team1, "team2": team2, "status": status, "winner": winner}


class TieBreakerGroupTests(unittest.TestCase):
    def test_a_clear_table_has_no_tie_breakers(self):
        rows = [row(1, points=6), row(2, points=4), row(3, points=2), row(4, points=0)]
        self.assertEqual(tie_breaker_groups(rows, 4), [])

    def test_net_coins_separate_teams_level_on_points_for_first(self):
        rows = [row(2, points=4, net=7), row(1, points=4, net=3), row(3, points=2), row(4, points=0)]
        (group,) = tie_breaker_groups(rows, 4)
        self.assertEqual((group["first_place"], group["last_place"]), (1, 2))
        self.assertEqual(group["pairs"][0]["decided_by"], "net_coins")
        self.assertEqual(group["pairs"][0]["upper_id"], 2)
        self.assertEqual(group["status"], "decided")
        self.assertEqual([team["rank"] for team in group["teams"]], [1, 2])

    def test_head_to_head_separates_teams_level_on_net_coins(self):
        rows = [row(1, points=4, head_to_head=1), row(2, points=4, head_to_head=0), row(3, points=2), row(4, points=0)]
        (group,) = tie_breaker_groups(rows, 4)
        self.assertEqual(group["pairs"][0]["decided_by"], "head_to_head")
        self.assertEqual(group["status"], "decided")

    def test_teams_level_on_every_breaker_wait_for_golden_strike(self):
        rows = [row(1, points=4), row(2, points=4), row(3, points=2), row(4, points=0)]
        (group,) = tie_breaker_groups(rows, 4)
        self.assertIsNone(group["pairs"][0]["decided_by"])
        self.assertEqual(group["pairs"][0]["status"], "golden_strike_due")
        self.assertEqual(group["status"], "golden_strike_due")
        self.assertEqual(group["golden_strikes"], [])

    def test_a_pending_golden_strike_between_the_pair_is_shown_as_pending(self):
        rows = [row(1, points=4), row(2, points=4), row(3, points=2), row(4, points=0)]
        group = tie_breaker_groups(rows, 4, [strike(9, 1, 2, status=1)])[0]
        self.assertEqual(group["pairs"][0]["status"], "golden_strike_pending")
        self.assertEqual(group["status"], "golden_strike_pending")
        self.assertEqual([match["match_id"] for match in group["golden_strikes"]], [9])

    def test_a_played_golden_strike_decides_the_pair(self):
        rows = [row(1, points=4, golden=1), row(2, points=4, golden=0), row(3, points=2), row(4, points=0)]
        group = tie_breaker_groups(rows, 4, [strike(9, 1, 2, status=3, winner=1)])[0]
        self.assertEqual(group["pairs"][0]["decided_by"], "golden_strike")
        self.assertEqual(group["status"], "decided")

    def test_a_tie_below_the_knockout_places_is_ignored(self):
        rows = [row(1, points=6), row(2, points=4), row(3, points=0), row(4, points=0)]
        self.assertEqual(tie_breaker_groups(rows, 4), [])

    def test_six_teams_show_the_tie_across_the_eliminator_cut(self):
        rows = [row(1, points=8), row(2, points=4), row(3, points=2), row(4, points=2), row(5, points=0), row(6, points=0)]
        groups = tie_breaker_groups(rows, 6)
        self.assertEqual([(group["first_place"], group["last_place"]) for group in groups], [(3, 4)])

    def test_a_three_way_tie_lists_each_pair_in_table_order(self):
        rows = [row(2, points=6, net=5), row(3, points=6, net=5), row(1, points=6, net=2), row(4, points=0)]
        (group,) = tie_breaker_groups(rows, 4)
        self.assertEqual(
            [(pair["upper_id"], pair["lower_id"], pair["decided_by"]) for pair in group["pairs"]],
            [(2, 3, None), (3, 1, "net_coins")],
        )
        self.assertEqual(group["status"], "golden_strike_due")


if __name__ == "__main__":
    unittest.main()
