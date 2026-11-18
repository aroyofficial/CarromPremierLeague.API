from schemas.settings_schema import CompetitionSettings
from services.settings_service import SettingsService, queen_needs_cover
import unittest


class TestQueenCover:
    def test_cover_is_kept_when_the_queen_is_used_in_league_matches(self):
        assert queen_needs_cover(True, True) is True

    def test_cover_is_cleared_when_the_queen_stays_off_the_league_board(self):
        assert queen_needs_cover(False, True) is False

    def test_cover_can_stay_off_while_the_queen_is_in_play(self):
        assert queen_needs_cover(True, False) is False


class TestAssistantSwitch:
    def test_the_assistant_stays_on_when_a_client_omits_the_switch(self):
        settings = CompetitionSettings(
            regulation_minutes=15,
            extra_minutes=5,
            league_queen=False,
            queen_needs_cover=False,
        )
        assert settings.assistant_enabled is True

    def test_update_stores_a_turned_off_assistant(self):
        saved = {}

        class Repository:
            def save(self, settings):
                saved["settings"] = settings
                return settings

        result = SettingsService(Repository()).update(
            CompetitionSettings(
                regulation_minutes=15,
                extra_minutes=5,
                league_queen=False,
                queen_needs_cover=True,
                assistant_enabled=False,
            )
        )
        assert result.assistant_enabled is False
        assert result.queen_needs_cover is False
        assert saved["settings"].assistant_enabled is False


class TestStartOnScheduledDay(unittest.TestCase):
    def test_the_switch_stays_off_when_a_client_omits_it(self):
        settings = CompetitionSettings(
            regulation_minutes=15,
            extra_minutes=5,
            league_queen=False,
            queen_needs_cover=False,
        )
        self.assertFalse(settings.start_on_scheduled_day)

    def test_update_stores_the_switch(self):
        saved = {}

        class Repository:
            def save(self, settings):
                saved["settings"] = settings
                return settings

        result = SettingsService(Repository()).update(
            CompetitionSettings(
                regulation_minutes=15,
                extra_minutes=5,
                league_queen=False,
                queen_needs_cover=False,
                start_on_scheduled_day=True,
            )
        )
        self.assertTrue(result.start_on_scheduled_day)
        self.assertTrue(saved["settings"].start_on_scheduled_day)
