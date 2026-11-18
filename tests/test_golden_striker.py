import unittest

from services.stats_service import NO_MATCH_PLAYED, NO_MATCH_SCHEDULED, golden_striker_state


class TestGoldenStrikerState(unittest.TestCase):
    def test_a_season_with_no_matches_uses_the_default_message(self):
        message, show = golden_striker_state(0, 0, 0)
        self.assertEqual(message, NO_MATCH_SCHEDULED)
        self.assertFalse(show)

    def test_scheduled_matches_that_are_not_played_hide_the_striker(self):
        message, show = golden_striker_state(4, 0, 3)
        self.assertEqual(message, NO_MATCH_PLAYED)
        self.assertFalse(show)

    def test_a_played_match_with_a_scorecard_shows_the_striker(self):
        message, show = golden_striker_state(4, 1, 2)
        self.assertTrue(show)
        self.assertIn("successfully", message)

    def test_a_played_match_without_a_scorecard_stays_hidden(self):
        _, show = golden_striker_state(1, 1, 0)
        self.assertFalse(show)
