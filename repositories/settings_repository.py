from schemas.settings_schema import CompetitionSettings

DEFAULTS = CompetitionSettings(
    regulation_minutes=15,
    extra_minutes=5,
    league_queen=False,
    queen_needs_cover=False,
)


class SettingsRepository:

    def __init__(self, db):
        self.db = db

    def get(self) -> CompetitionSettings:
        cursor = self.db.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT TOP (1)
                RegulationMinutes,
                ExtraMinutes,
                LeagueQueen,
                QueenNeedsCover
            FROM tblSettings
            WHERE Id = 1
            """
        )
        row = cursor.fetchone()
        if not row:
            return CompetitionSettings(
                regulation_minutes=DEFAULTS.regulation_minutes,
                extra_minutes=DEFAULTS.extra_minutes,
                league_queen=DEFAULTS.league_queen,
                queen_needs_cover=DEFAULTS.queen_needs_cover,
            )
        return CompetitionSettings(
            regulation_minutes=int(row["RegulationMinutes"]),
            extra_minutes=int(row["ExtraMinutes"]),
            league_queen=bool(row["LeagueQueen"]),
            queen_needs_cover=bool(row["QueenNeedsCover"]),
        )

    def save(self, settings: CompetitionSettings) -> CompetitionSettings:
        cursor = self.db.cursor()
        cursor.execute(
            """
            UPDATE tblSettings
            SET RegulationMinutes = %s,
                ExtraMinutes = %s,
                LeagueQueen = %s,
                QueenNeedsCover = %s,
                UpdatedAt = SYSUTCDATETIME()
            WHERE Id = 1
            """,
            (
                settings.regulation_minutes,
                settings.extra_minutes,
                int(settings.league_queen),
                int(settings.queen_needs_cover),
            ),
        )
        if cursor.rowcount == 0:
            cursor.execute(
                """
                INSERT INTO tblSettings (
                    Id, RegulationMinutes, ExtraMinutes, LeagueQueen, QueenNeedsCover
                )
                VALUES (1, %s, %s, %s, %s)
                """,
                (
                    settings.regulation_minutes,
                    settings.extra_minutes,
                    int(settings.league_queen),
                    int(settings.queen_needs_cover),
                ),
            )
        self.db.commit()
        return self.get()
