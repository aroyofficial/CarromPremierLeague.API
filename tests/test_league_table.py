from datetime import date

from repositories.season_repository import arrange_standings, show_placeholder_table
from schemas.season_schema import LeagueTableStanding


def standing(team_id, team_name, played=0, points=0, net_points=0):
    return LeagueTableStanding(
        team_id=team_id,
        team_name=team_name,
        matches_played=played,
        wins=0,
        points=points,
        net_points=net_points,
        head_to_head_wins=0,
    )


class TestPlaceholderTable:
    def test_future_season_with_no_results_stays_blank(self):
        assert show_placeholder_table(date(2026, 12, 27), date(2026, 11, 13), 0) is True

    def test_completed_match_updates_the_table_before_the_start_date(self):
        assert show_placeholder_table(date(2026, 12, 27), date(2026, 11, 13), 2) is False

    def test_started_season_uses_recorded_results(self):
        assert show_placeholder_table(date(2026, 1, 25), date(2026, 11, 13), 0) is False

    def test_missing_start_date_uses_recorded_results(self):
        assert show_placeholder_table(None, date(2026, 11, 13), 0) is False


class TestArrangeStandings:
    def test_no_results_are_alphabetical_and_keep_every_team(self):
        rows = arrange_standings([
            standing(2, "Sasthitala Smashers"),
            standing(1, "Dum Dum Diamonds"),
            standing(3, "Nischinda Ninjas"),
        ])
        assert [row.team_name for row in rows] == [
            "Dum Dum Diamonds",
            "Nischinda Ninjas",
            "Sasthitala Smashers",
        ]

    def test_unplayed_teams_stay_on_a_table_that_has_results(self):
        rows = arrange_standings([
            standing(1, "Dum Dum Diamonds", played=1, points=2, net_points=5),
            standing(2, "Nischinda Ninjas"),
        ])
        assert [row.team_name for row in rows] == ["Dum Dum Diamonds", "Nischinda Ninjas"]
        assert rows[1].matches_played == 0
