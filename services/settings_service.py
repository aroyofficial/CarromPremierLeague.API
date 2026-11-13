from schemas.settings_schema import CompetitionSettings


def queen_needs_cover(league_queen: bool, requested_cover: bool) -> bool:
    """A cover is only meaningful when the Queen is used in league matches."""
    return bool(league_queen) and bool(requested_cover)


class SettingsService:

    def __init__(self, repository):
        self.repository = repository

    def get(self) -> CompetitionSettings:
        return self.repository.get()

    def update(self, request: CompetitionSettings) -> CompetitionSettings:
        stored = CompetitionSettings(
            regulation_minutes=request.regulation_minutes,
            extra_minutes=request.extra_minutes,
            league_queen=request.league_queen,
            queen_needs_cover=queen_needs_cover(request.league_queen, request.queen_needs_cover),
        )
        return self.repository.save(stored)
