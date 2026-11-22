from types import SimpleNamespace
import unittest
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

from enums.match_category import MatchCategory
from enums.match_outcome import MatchOutcome
from enums.match_status import MatchStatus
from repositories.match_repository import MatchRepository
from schemas.match_schema import MatchResponse, MatchUpdateRequest
from services.match_service import MatchService
from services.match_rules import (
    EXTRA_SECONDS,
    REGULATION_SECONDS,
    both_colours_cleared,
    colour_scores,
    knockout_state,
    validate_lifecycle_update,
)


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
        self.assertTrue(any("ConcludedAt = NULL" in sql for sql, _ in executed))

    def test_only_a_finished_match_can_be_marked_complete_or_reopened(self):
        finished = live_match(MatchCategory.Eliminator)
        finished.status = MatchStatus.Played.value
        # A stored result is not re-judged when the stream only moves on.
        finished.outcome = MatchOutcome.NotDecided.value
        validate_lifecycle_update(finished, {"is_concluded": True})
        validate_lifecycle_update(finished, {"is_concluded": False})

        for status in (MatchStatus.Scheduled.value, MatchStatus.InProgress.value):
            unfinished = live_match(MatchCategory.Final)
            unfinished.status = status
            with self.assertRaises(HTTPException) as raised:
                validate_lifecycle_update(unfinished, {"is_concluded": True})
            self.assertIn("finished match", raised.exception.detail)

    def test_repository_records_and_clears_the_completion_stamp(self):
        executed = []

        class FakeCursor:
            def execute(self, sql, params=None):
                executed.append(" ".join(sql.split()))

            def fetchone(self):
                return {
                    "Id": 7, "Team1": 1, "Team2": 2, "ScheduledDate": date(2026, 11, 21),
                    "Duration": None, "Extra": None, "GoldenStrike": 0, "Category": 3,
                    "Status": 3, "SeasonId": 3, "NetPoints": None, "Outcome": 1, "Order": 4,
                    "TossOutcome": 1, "ConcludedAt": None,
                }

        class FakeDb:
            def cursor(self, dictionary=False):
                return FakeCursor()

            def commit(self):
                pass

        repo = MatchRepository(FakeDb())
        repo.update(7, MatchUpdateRequest(status=MatchStatus.InProgress.value))
        self.assertTrue(
            any("SET ConcludedAt = SYSUTCDATETIME()" in sql and "ConcludedAt IS NULL" in sql for sql in executed)
        )
        executed.clear()
        repo.update(7, MatchUpdateRequest(is_concluded=True))
        self.assertTrue(any("ConcludedAt = COALESCE(ConcludedAt, SYSUTCDATETIME())" in sql for sql in executed))
        executed.clear()
        repo.update(7, MatchUpdateRequest(is_concluded=False))
        self.assertTrue(any("ConcludedAt = NULL" in sql for sql in executed))


def played(category, **overrides):
    """A live knockout match that is being completed."""
    update = {
        "status": MatchStatus.Played.value,
        "duration": REGULATION_SECONDS,
        "extra": None,
        "outcome": MatchOutcome.NotDecided.value,
    }
    update.update(overrides)
    return live_match(category), update


class KnockoutScorecardRuleTests(unittest.TestCase):
    def reject(self, match, update, scores, fragment, **kwargs):
        with self.assertRaises(HTTPException) as raised:
            validate_lifecycle_update(match, update, scores=scores, **kwargs)
        self.assertIn(fragment, raised.exception.detail)

    def test_both_teams_on_nine_cannot_be_recorded_in_a_knockout_match(self):
        match, update = played(MatchCategory.Final, outcome=MatchOutcome.Team1Win.value, net_points=0)
        self.reject(match, update, (9, 9), "Both teams cannot", colour_cleared=True)
        self.assertTrue(both_colours_cleared((9, 9)))
        self.assertFalse(both_colours_cleared((9, 8)))

    def test_a_level_knockout_scorecard_cannot_name_a_winner_without_a_golden_strike(self):
        match, update = played(MatchCategory.Eliminator, extra=EXTRA_SECONDS, outcome=MatchOutcome.Team1Win.value)
        self.reject(match, update, (5, 5), "Golden Strike")

    def test_a_level_knockout_scorecard_can_be_recorded_as_a_tie_after_either_choice(self):
        for extra in (None, EXTRA_SECONDS):
            with self.subTest(extra=extra):
                match, update = played(MatchCategory.Qualifier2, extra=extra)
                validate_lifecycle_update(match, update, scores=(4, 4))

    def test_a_level_knockout_scorecard_goes_to_a_golden_strike_after_extra_time(self):
        match, update = played(
            MatchCategory.Final,
            extra=EXTRA_SECONDS,
            golden_strike=True,
            outcome=MatchOutcome.Team2Win.value,
        )
        validate_lifecycle_update(match, update, scores=(6, 6))

    def test_a_golden_strike_needs_a_level_scorecard(self):
        match, update = played(
            MatchCategory.Final,
            extra=EXTRA_SECONDS,
            golden_strike=True,
            outcome=MatchOutcome.Team1Win.value,
        )
        self.reject(match, update, (6, 5), "only when the scorecard is level")

    def test_the_result_has_to_follow_the_scorecard(self):
        match, update = played(MatchCategory.Final, outcome=MatchOutcome.Team1Win.value, net_points=2)
        validate_lifecycle_update(match, update, scores=(7, 5))
        self.reject(match, update, (5, 7), "leads the scorecard")
        tied_update = dict(update, outcome=MatchOutcome.NotDecided.value)
        self.reject(match, tied_update, (7, 5), "leads the scorecard")

    def test_extra_time_starts_only_when_the_scorecard_is_level(self):
        match = live_match(MatchCategory.Final)
        match.extra_started_at = None
        validate_lifecycle_update(match, {"extra": 0}, scores=(3, 3))
        self.reject(match, {"extra": 0}, (4, 3), "level after regulation")
        self.reject(match, {"extra": 0}, (9, 3), "cleared its 9 coins")

    def test_golden_strike_can_be_chosen_live_only_after_extra_time_with_a_level_scorecard(self):
        match = live_match(MatchCategory.Final)
        match.extra_started_at = None
        self.reject(match, {"golden_strike": True}, (3, 3), "after extra time has been played")
        match.extra_started_at = "2026-11-22T10:00:00"
        validate_lifecycle_update(match, {"golden_strike": True}, scores=(3, 3))
        self.reject(match, {"golden_strike": True}, (4, 3), "scorecard is level")
        league = live_match(MatchCategory.League)
        self.reject(league, {"golden_strike": True}, (3, 3), "knockout")

    def test_league_results_are_not_judged_against_the_knockout_rules(self):
        match, update = played(MatchCategory.League, outcome=MatchOutcome.NotDecided.value)
        validate_lifecycle_update(match, update, scores=(9, 9))


class KnockoutStateTests(unittest.TestCase):
    start = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)

    def state(self, scores, now_seconds, **kwargs):
        category = kwargs.pop("category", MatchCategory.Final.value)
        status = kwargs.pop("status", MatchStatus.InProgress.value)
        return knockout_state(
            category=category,
            status=status,
            scores=scores,
            started_at=kwargs.pop("started_at", self.start),
            now=self.start + timedelta(seconds=now_seconds),
            **kwargs,
        )

    def test_nothing_is_asked_during_regulation(self):
        self.assertIsNone(self.state((4, 4), REGULATION_SECONDS - 1)["decision"])

    def test_a_level_match_asks_about_extra_time_when_regulation_ends(self):
        state = self.state((4, 4), REGULATION_SECONDS)
        self.assertTrue(state["level"])
        self.assertEqual(state["decision"], "extra_time")

    def test_a_match_with_a_leader_asks_nothing(self):
        state = self.state((5, 4), REGULATION_SECONDS + 60)
        self.assertFalse(state["level"])
        self.assertIsNone(state["decision"])

    def test_a_level_match_asks_about_a_golden_strike_when_extra_time_ends(self):
        extra_start = self.start + timedelta(seconds=REGULATION_SECONDS)
        during = self.state((4, 4), REGULATION_SECONDS + EXTRA_SECONDS - 1, extra_started_at=extra_start)
        self.assertIsNone(during["decision"])
        over = self.state((4, 4), REGULATION_SECONDS + EXTRA_SECONDS, extra_started_at=extra_start)
        self.assertEqual(over["decision"], "golden_strike")

    def test_a_chosen_golden_strike_is_not_asked_again(self):
        extra_start = self.start + timedelta(seconds=REGULATION_SECONDS)
        state = self.state(
            (4, 4), REGULATION_SECONDS + EXTRA_SECONDS + 5, extra_started_at=extra_start, golden_strike=True
        )
        self.assertIsNone(state["decision"])

    def test_league_matches_and_unscored_matches_ask_nothing(self):
        self.assertIsNone(self.state((4, 4), REGULATION_SECONDS, category=MatchCategory.League.value)["decision"])
        self.assertIsNone(self.state(None, REGULATION_SECONDS)["decision"])

    def test_a_finished_knockout_match_with_no_winner_is_tied(self):
        tied = self.state((4, 4), 0, status=MatchStatus.Played.value, outcome=MatchOutcome.NotDecided.value)
        self.assertTrue(tied["tied"])
        won = self.state((5, 4), 0, status=MatchStatus.Played.value, outcome=MatchOutcome.Team1Win.value)
        self.assertFalse(won["tied"])
        golden = self.state(
            (4, 4), 0, status=MatchStatus.Played.value, outcome=MatchOutcome.Team1Win.value, golden_strike=True
        )
        self.assertFalse(golden["tied"])


class ScorecardSaveTests(unittest.TestCase):
    """The scorecard itself refuses both teams on 9 in a knockout match."""

    def service(self):
        class Repo:
            db = None

            def players_on_match_teams(self, match_id):
                return [{"PlayerId": 1, "TeamId": 10}, {"PlayerId": 2, "TeamId": 20}]

            def player_coin_lines(self, match_id):
                return []

        return MatchService(Repo())

    @staticmethod
    def line(player_id, own, opponent=0):
        return SimpleNamespace(
            player_id=player_id, coins_pocketed=own, opponent_coins_pocketed=opponent, coins_fined=0
        )

    def test_both_colours_cleared_is_refused_in_a_knockout_match(self):
        service = self.service()
        stats = [self.line(1, 9), self.line(2, 9)]
        for category in (MatchCategory.Eliminator, MatchCategory.Final):
            with self.subTest(category=category), self.assertRaises(HTTPException) as raised:
                service._validate_coin_counts(1, stats, category.value)
            self.assertIn("Both teams cannot be on 9", raised.exception.detail)

    def test_both_colours_cleared_is_not_judged_outside_knockout_matches(self):
        self.service()._validate_coin_counts(1, [self.line(1, 9), self.line(2, 9)], MatchCategory.League.value)

    def test_one_cleared_colour_is_fine_in_a_knockout_match(self):
        self.service()._validate_coin_counts(1, [self.line(1, 9), self.line(2, 8)], MatchCategory.Final.value)

    def test_coins_taken_from_the_other_colour_count_toward_nine(self):
        # Each team pocketed 5 of its own colour and 4 of the other's: both colours are on 5 + 4 = 9.
        service = self.service()
        stats = [self.line(1, 5, opponent=4), self.line(2, 5, opponent=4)]
        with self.assertRaises(HTTPException):
            service._validate_coin_counts(1, stats, MatchCategory.Final.value)


class ScorecardScoreTests(unittest.TestCase):
    def test_a_colour_counts_own_coins_less_fines_plus_the_coins_the_other_team_took(self):
        scores = colour_scores(
            {
                "a": {"own": 5, "opponent": 1, "fines": 2},
                "b": {"own": 4, "opponent": 2, "fines": 0},
            }
        )
        self.assertEqual(scores, {"a": 3 + 2, "b": 4 + 1})

    def test_a_fine_that_cannot_be_paid_returns_nothing(self):
        scores = colour_scores({"a": {"own": 1, "opponent": 0, "fines": 4}, "b": {"own": 0, "opponent": 0, "fines": 0}})
        self.assertEqual(scores["a"], 0)

