from datetime import date

from repositories.season_repository import show_placeholder_table


class TestPlaceholderTable:
    def test_future_season_with_no_results_stays_blank(self):
        assert show_placeholder_table(date(2026, 12, 27), date(2026, 11, 13), 0) is True

    def test_completed_match_updates_the_table_before_the_start_date(self):
        assert show_placeholder_table(date(2026, 12, 27), date(2026, 11, 13), 2) is False

    def test_started_season_uses_recorded_results(self):
        assert show_placeholder_table(date(2026, 1, 25), date(2026, 11, 13), 0) is False

    def test_missing_start_date_uses_recorded_results(self):
        assert show_placeholder_table(None, date(2026, 11, 13), 0) is False
