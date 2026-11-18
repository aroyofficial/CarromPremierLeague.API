from pydantic import BaseModel, Field, model_validator


class CompetitionSettings(BaseModel):
    regulation_minutes: int = Field(ge=5, le=30)
    extra_minutes: int = Field(ge=1, le=10)
    league_queen: bool
    queen_needs_cover: bool
    # Off leaves a match startable on any day. On allows the start only on its scheduled date.
    start_on_scheduled_day: bool = False
    # Striker has one switch for the public site and one for the admin console.
    assistant_public_enabled: bool | None = None
    assistant_admin_enabled: bool | None = None
    # The old single switch. Older clients still send and read it: a value sent without the two
    # new switches sets both, and a response carries "on" while Striker is on for anyone.
    assistant_enabled: bool | None = None

    @model_validator(mode="after")
    def _settle_assistant_switches(self):
        legacy = self.assistant_enabled
        if self.assistant_public_enabled is None:
            self.assistant_public_enabled = True if legacy is None else legacy
        if self.assistant_admin_enabled is None:
            self.assistant_admin_enabled = True if legacy is None else legacy
        self.assistant_enabled = self.assistant_public_enabled or self.assistant_admin_enabled
        return self
