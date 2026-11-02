from types import SimpleNamespace
import unittest

from fastapi import HTTPException

from enums.match_category import MatchCategory
from enums.match_outcome import MatchOutcome
from enums.match_status import MatchStatus
from services.match_rules import EXTRA_SECONDS, REGULATION_SECONDS, validate_lifecycle_update


def live_match(category):
    return SimpleNamespace(
        category=category.value,
        status=MatchStatus.InProgress.value,
        team1=1,
        team2=2,
    )


class MatchLifecycleRuleTests(unittest.TestCase):
    def test_league_match_can_finish_level_after_full_regulation(self):
        validate_lifecycle_update(
            live_match(MatchCategory.League),
            {
                "status": MatchStatus.Played.value,
                "duration": REGULATION_SECONDS,
                "outcome": MatchOutcome.NotDecided.value,
                "extra": None,
            },
        )

    def test_knockout_draw_requires_full_extra_time(self):
        for category in (
            MatchCategory.Eliminator,
            MatchCategory.Qualifier1,
            MatchCategory.Qualifier2,
            MatchCategory.Final,
        ):
            with self.subTest(category=category):
                validate_lifecycle_update(
                    live_match(category),
                    {
                        "status": MatchStatus.Played.value,
                        "duration": REGULATION_SECONDS,
                        "extra": EXTRA_SECONDS,
                        "outcome": MatchOutcome.NotDecided.value,
                    },
                )

    def test_league_match_rejects_extra_time(self):
        with self.assertRaises(HTTPException) as raised:
            validate_lifecycle_update(live_match(MatchCategory.League), {"extra": 0})
        self.assertEqual(raised.exception.detail, "League matches do not have extra time.")

    def test_scheduled_match_cannot_receive_a_result(self):
        match = live_match(MatchCategory.League)
        match.status = MatchStatus.Scheduled.value
        with self.assertRaises(HTTPException) as raised:
            validate_lifecycle_update(match, {"outcome": MatchOutcome.Team1Win.value})
        self.assertIn("cannot be recorded", raised.exception.detail)

    def test_knockout_draw_can_finish_when_extra_time_is_declined(self):
        validate_lifecycle_update(
            live_match(MatchCategory.Qualifier1),
            {
                "status": MatchStatus.Played.value,
                "duration": REGULATION_SECONDS,
                "extra": None,
                "outcome": MatchOutcome.NotDecided.value,
            },
        )

    def test_finished_scorecard_can_be_corrected(self):
        match = live_match(MatchCategory.League)
        match.status = MatchStatus.Played.value
        match.outcome = MatchOutcome.Team2Win.value
        match.duration = REGULATION_SECONDS
        match.extra = None
        match.golden_strike = False
        validate_lifecycle_update(match, {"outcome": MatchOutcome.Team1Win.value, "net_points": 2})

    def test_golden_strike_requires_a_winner_and_no_clock(self):
        validate_lifecycle_update(
            live_match(MatchCategory.GoldenStrike),
            {
                "status": MatchStatus.Played.value,
                "duration": 0,
                "extra": None,
                "outcome": MatchOutcome.Team1Win.value,
            },
        )
