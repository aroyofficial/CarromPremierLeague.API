from types import SimpleNamespace
import unittest
from datetime import date

from fastapi import HTTPException

from enums.match_category import MatchCategory
from enums.match_outcome import MatchOutcome
from enums.match_status import MatchStatus
from repositories.match_repository import MatchRepository
from schemas.match_schema import MatchResponse, MatchUpdateRequest
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

    def test_league_match_can_finish_early_once_a_team_reaches_nine(self):
        validate_lifecycle_update(
            live_match(MatchCategory.League),
            {
                "status": MatchStatus.Played.value,
                "duration": 8 * 60,
                "outcome": MatchOutcome.Team1Win.value,
                "net_points": 9,
                "extra": None,
            },
            colour_cleared=True,
        )

    def test_league_match_still_requires_full_time_below_nine(self):
        with self.assertRaises(HTTPException) as raised:
            validate_lifecycle_update(
                live_match(MatchCategory.League),
                {
                    "status": MatchStatus.Played.value,
                    "duration": 8 * 60,
                    "outcome": MatchOutcome.Team1Win.value,
                    "extra": None,
                },
            )
        self.assertIn("15-minute", raised.exception.detail)

    def test_a_match_starts_only_on_its_scheduled_day_when_the_setting_is_on(self):
        match = live_match(MatchCategory.League)
        match.status = MatchStatus.Scheduled.value
        match.scheduled_date = date(2026, 12, 27)
        validate_lifecycle_update(
            match,
            {"status": MatchStatus.InProgress.value},
            start_on_scheduled_day=True,
            today=date(2026, 12, 27),
        )
        with self.assertRaises(HTTPException) as raised:
            validate_lifecycle_update(
                match,
                {"status": MatchStatus.InProgress.value},
                start_on_scheduled_day=True,
                today=date(2026, 12, 26),
            )
        self.assertIn("scheduled date", raised.exception.detail)

    def test_a_match_can_start_on_another_day_when_the_setting_is_off(self):
        match = live_match(MatchCategory.League)
        match.status = MatchStatus.Scheduled.value
        match.scheduled_date = date(2026, 12, 27)
        validate_lifecycle_update(
            match,
            {"status": MatchStatus.InProgress.value},
            start_on_scheduled_day=False,
            today=date(2026, 12, 26),
        )

    def test_knockout_can_finish_during_extra_time_once_a_team_reaches_nine(self):
        validate_lifecycle_update(
            live_match(MatchCategory.Final),
            {
                "status": MatchStatus.Played.value,
                "duration": REGULATION_SECONDS,
                "extra": 2 * 60,
                "outcome": MatchOutcome.Team2Win.value,
                "net_points": 3,
            },
            colour_cleared=True,
        )

    def test_finished_early_match_can_keep_its_short_duration(self):
        match = live_match(MatchCategory.League)
        match.status = MatchStatus.Played.value
        match.outcome = MatchOutcome.Team1Win.value
        match.duration = 8 * 60
        match.extra = None
        match.golden_strike = False
        validate_lifecycle_update(match, {"net_points": 9})

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

    def test_scheduled_match_with_both_teams_can_be_marked_and_cleared_ready(self):
        match = live_match(MatchCategory.League)
        match.status = MatchStatus.Scheduled.value
        validate_lifecycle_update(match, {"is_ready": True})
        validate_lifecycle_update(match, {"is_ready": False})

    def test_ready_requires_both_teams_and_scheduled_status(self):
        unassigned = SimpleNamespace(
            category=MatchCategory.Final.value,
            status=MatchStatus.Scheduled.value,
            team1=1,
            team2=None,
        )
        with self.assertRaises(HTTPException) as raised:
            validate_lifecycle_update(unassigned, {"is_ready": True})
        self.assertIn("Both teams", raised.exception.detail)

        in_progress = live_match(MatchCategory.League)
        with self.assertRaises(HTTPException) as raised_live:
            validate_lifecycle_update(in_progress, {"is_ready": True})
        self.assertIn("scheduled match", raised_live.exception.detail)

    def test_repository_sets_ready_clears_other_scheduled_matches_and_records_completed_at(self):
        executed = []

        class FakeCursor:
            def execute(self, sql, params=None):
                executed.append((" ".join(sql.split()), params))

            def fetchone(self):
                return {
                    "Id": 7,
                    "Team1": 1,
                    "Team2": 2,
                    "ScheduledDate": date(2026, 11, 21),
                    "Duration": None,
                    "Extra": None,
                    "GoldenStrike": 0,
                    "Category": 1,
                    "Status": 1,
                    "SeasonId": 3,
                    "NetPoints": None,
                    "Outcome": 3,
                    "Order": 4,
                    "TossOutcome": 3,
                    "ReadyAt": None,
                    "StartedAt": None,
                    "ExtraStartedAt": None,
                    "CompletedAt": None,
                }

        class FakeDb:
            def cursor(self, dictionary=False):
                return FakeCursor()

            def commit(self):
                executed.append(("COMMIT", None))

        repo = MatchRepository(FakeDb())
        repo.update(7, MatchUpdateRequest(is_ready=True))
        self.assertTrue(any("SET ReadyAt = NULL" in sql and "Id <> %s" in sql for sql, _ in executed))
        self.assertTrue(any("ReadyAt = COALESCE(ReadyAt, SYSUTCDATETIME())" in sql for sql, _ in executed))

        executed.clear()
        repo.update(
            7,
            MatchUpdateRequest(
                status=MatchStatus.Played.value,
                duration=REGULATION_SECONDS,
                outcome=MatchOutcome.Team1Win.value,
                net_points=4,
            ),
        )
        self.assertTrue(any("CompletedAt = COALESCE(CompletedAt, SYSUTCDATETIME())" in sql for sql, _ in executed))

        executed.clear()
        repo.reset(7)
        self.assertTrue(any("ReadyAt = NULL" in sql and "CompletedAt = NULL" in sql for sql, _ in executed))

