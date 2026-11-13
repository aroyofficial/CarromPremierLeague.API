from services.settings_service import queen_needs_cover


class TestQueenCover:
    def test_cover_is_kept_when_the_queen_is_used_in_league_matches(self):
        assert queen_needs_cover(True, True) is True

    def test_cover_is_cleared_when_the_queen_stays_off_the_league_board(self):
        assert queen_needs_cover(False, True) is False

    def test_cover_can_stay_off_while_the_queen_is_in_play(self):
        assert queen_needs_cover(True, False) is False
