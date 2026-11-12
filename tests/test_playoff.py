import unittest

from services.playoff import knockout_fixtures, qualification_tie_groups


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

    def test_more_than_four_teams_get_an_eliminator_and_a_tbd_final(self):
        self.assertEqual(
            knockout_fixtures(6, [1, 2, 3, 4, 5, 6], None, False),
            [("eliminator", 2, 3), ("final", None, None)],
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


if __name__ == "__main__":
    unittest.main()
