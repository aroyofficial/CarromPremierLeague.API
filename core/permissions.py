from dataclasses import dataclass


@dataclass(frozen=True)
class Permission:
    code: str
    area: str
    name: str
    description: str
    sort_order: int


# CarromPremierLeague.Database setup.sql seeds the same rows; keep both lists in step.
CATALOG: tuple[Permission, ...] = (
    Permission("seasons.create", "Seasons", "Create seasons", "Add a new season.", 10),
    Permission("seasons.update", "Seasons", "Edit seasons", "Change season names, dates and public visibility.", 20),
    Permission("seasons.delete", "Seasons", "Delete seasons", "Remove a season.", 30),
    Permission("seasons.teams", "Seasons", "Choose season teams", "Add teams to a season or remove them.", 40),
    Permission("teams.create", "Teams", "Create teams", "Add a new team.", 110),
    Permission("teams.update", "Teams", "Edit teams", "Change team names, slogans and logos.", 120),
    Permission("teams.delete", "Teams", "Delete teams", "Remove a team.", 130),
    Permission("players.create", "Players", "Create players", "Add a new player profile.", 210),
    Permission("players.update", "Players", "Edit players", "Change player profiles and photos.", 220),
    Permission("players.delete", "Players", "Delete players", "Remove a player profile.", 230),
    Permission("countries.create", "Countries", "Create countries", "Add a nationality to the reference list.", 310),
    Permission("countries.update", "Countries", "Edit countries", "Change a country name or flag.", 320),
    Permission("countries.delete", "Countries", "Delete countries", "Remove a country.", 330),
    Permission("rosters.manage", "Rosters", "Manage rosters", "Assign players to teams for a season and remove them.", 410),
    Permission("matches.create", "Matches", "Create matches", "Schedule new fixtures.", 510),
    Permission(
        "matches.update",
        "Matches",
        "Edit and run matches",
        "Change fixture details, call the toss and run a match live.",
        520,
    ),
    Permission("matches.scorecard", "Matches", "Save scorecards", "Save player statistics for a match.", 530),
    Permission("matches.reset", "Matches", "Reset matches", "Return a started or played match to scheduled.", 540),
    Permission("matches.delete", "Matches", "Delete matches", "Remove a fixture.", 550),
    Permission("settings.update", "Settings", "Change competition settings", "Set the match clock and the Queen rules.", 610),
    Permission("admins.view", "Admin users", "View admin users", "See the list of admin users and what each can do.", 710),
    Permission("admins.create", "Admin users", "Create admin users", "Add a new admin user.", 720),
    Permission(
        "admins.update",
        "Admin users",
        "Edit admin users",
        "Rename other admins, reset their passwords and turn accounts on or off.",
        730,
    ),
    Permission("admins.delete", "Admin users", "Delete admin users", "Remove another admin user.", 740),
    Permission(
        "permissions.manage",
        "Permissions",
        "Manage permissions",
        "Grant and revoke permissions you hold for any admin user.",
        810,
    ),
)

ALL_CODES: frozenset[str] = frozenset(permission.code for permission in CATALOG)
NAMES: dict[str, str] = {permission.code: permission.name for permission in CATALOG}
MANAGE_PERMISSIONS = "permissions.manage"
VIEW_ADMINS = "admins.view"
CREATE_ADMINS = "admins.create"
UPDATE_ADMINS = "admins.update"
DELETE_ADMINS = "admins.delete"


def ensure_known(*codes: str) -> None:
    unknown = [code for code in codes if code not in ALL_CODES]
    if unknown:
        raise ValueError(f"Unknown permission code(s): {', '.join(unknown)}")


def ordered(codes) -> list[str]:
    wanted = set(codes)
    return [permission.code for permission in CATALOG if permission.code in wanted]


def describe(codes) -> str:
    return ", ".join(f"'{NAMES.get(code, code)}'" for code in ordered(codes))


def denied_message(codes) -> str:
    if len(codes) == 1:
        return f"You need the {describe(codes)} permission to do this."
    return f"You need one of these permissions to do this: {describe(codes)}."
