from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    code: str
    name: str
    description: str
    sort_order: int


@dataclass(frozen=True)
class Area:
    code: str
    section: str
    name: str
    description: str
    icon: str
    sort_order: int


@dataclass(frozen=True)
class Permission:
    code: str
    area: str
    name: str
    description: str
    sort_order: int
    parent: str | None = None


@dataclass(frozen=True)
class Role:
    code: str
    name: str
    description: str
    sort_order: int
    permissions: tuple[str, ...]


# CarromPremierLeague.Database setup.sql seeds the same rows; keep both lists in step.
SECTIONS: tuple[Section, ...] = (
    Section("league", "League setup", "Seasons, teams, players and who plays where.", 10),
    Section("matchday", "Match day", "Fixtures, live scoring and competition rules.", 20),
    Section("administration", "Administration", "Who can sign in and what they can do.", 30),
)

AREAS: tuple[Area, ...] = (
    Area("seasons", "league", "Seasons", "Season calendar and the teams taking part.", "Calendar", 10),
    Area("teams", "league", "Teams", "Team profiles and logos.", "Flag", 20),
    Area("players", "league", "Players", "Player profiles and photos.", "User", 30),
    Area("countries", "league", "Countries", "Nationalities players can pick from.", "Location", 40),
    Area("rosters", "league", "Rosters", "Which players play for which team each season.", "List", 50),
    Area("matches", "matchday", "Matches", "Fixtures, tosses, live play and scorecards.", "Trophy", 60),
    Area("settings", "matchday", "Settings", "Match clock and Queen rules.", "Setting", 70),
    Area("admins", "administration", "Admin users", "Accounts that can sign in to this console.", "UserFilled", 80),
    Area("permissions", "administration", "Permissions", "Granting and removing permissions.", "Lock", 90),
)

CATALOG: tuple[Permission, ...] = (
    Permission("seasons.create", "seasons", "Create seasons", "Add a new season.", 10),
    Permission("seasons.update", "seasons", "Edit seasons", "Change season names, dates and public visibility.", 20),
    Permission("seasons.delete", "seasons", "Delete seasons", "Remove a season.", 30, "seasons.update"),
    Permission("seasons.teams", "seasons", "Choose season teams", "Add teams to a season or remove them.", 40, "seasons.update"),
    Permission("teams.create", "teams", "Create teams", "Add a new team.", 110),
    Permission("teams.update", "teams", "Edit teams", "Change team names, slogans and logos.", 120),
    Permission("teams.delete", "teams", "Delete teams", "Remove a team.", 130, "teams.update"),
    Permission("players.create", "players", "Create players", "Add a new player profile.", 210),
    Permission("players.update", "players", "Edit players", "Change player profiles and photos.", 220),
    Permission("players.delete", "players", "Delete players", "Remove a player profile.", 230, "players.update"),
    Permission("countries.create", "countries", "Create countries", "Add a nationality to the reference list.", 310),
    Permission("countries.update", "countries", "Edit countries", "Change a country name or flag.", 320),
    Permission("countries.delete", "countries", "Delete countries", "Remove a country.", 330, "countries.update"),
    Permission("rosters.manage", "rosters", "Manage rosters", "Assign players to teams for a season and remove them.", 410),
    Permission("matches.create", "matches", "Create matches", "Schedule new fixtures.", 510),
    Permission(
        "matches.update",
        "matches",
        "Edit and run matches",
        "Change fixture details, call the toss and run a match live.",
        520,
    ),
    Permission("matches.scorecard", "matches", "Save scorecards", "Save player statistics for a match.", 530, "matches.update"),
    Permission("matches.reset", "matches", "Reset matches", "Return a started or played match to scheduled.", 540, "matches.update"),
    Permission("matches.delete", "matches", "Delete matches", "Remove a fixture.", 550, "matches.update"),
    Permission("settings.update", "settings", "Change competition settings", "Set the match clock and the Queen rules.", 610),
    Permission("admins.view", "admins", "View admin users", "See the list of admin users and what each can do.", 710),
    Permission("admins.create", "admins", "Create admin users", "Add a new admin user.", 720),
    Permission(
        "admins.update",
        "admins",
        "Edit admin users",
        "Rename other admins, reset their passwords and turn accounts on or off.",
        730,
        "admins.view",
    ),
    Permission("admins.delete", "admins", "Delete admin users", "Remove another admin user.", 740, "admins.view"),
    Permission(
        "permissions.manage",
        "permissions",
        "Manage permissions",
        "Grant and revoke permissions you hold for any admin user.",
        810,
    ),
)

ROLES: tuple[Role, ...] = (
    Role(
        "match_day",
        "Match day operator",
        "Runs matches live, saves scorecards and can reset a match.",
        10,
        ("matches.update", "matches.scorecard", "matches.reset"),
    ),
    Role(
        "season_planner",
        "Season planner",
        "Sets up seasons, picks their teams, fills rosters and schedules fixtures.",
        20,
        ("seasons.create", "seasons.update", "seasons.teams", "rosters.manage", "matches.create"),
    ),
    Role(
        "content_editor",
        "League content editor",
        "Keeps teams, players, countries and rosters up to date.",
        30,
        (
            "teams.create",
            "teams.update",
            "players.create",
            "players.update",
            "countries.create",
            "countries.update",
            "rosters.manage",
        ),
    ),
    Role(
        "account_manager",
        "Account manager",
        "Adds admin users and keeps their accounts in order.",
        40,
        ("admins.view", "admins.create", "admins.update"),
    ),
)

ALL_CODES: frozenset[str] = frozenset(permission.code for permission in CATALOG)
NAMES: dict[str, str] = {permission.code: permission.name for permission in CATALOG}
AREA_NAMES: dict[str, str] = {area.code: area.name for area in AREAS}
PARENTS: dict[str, str] = {permission.code: permission.parent for permission in CATALOG if permission.parent}
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


def parent_of(code: str) -> str | None:
    return PARENTS.get(code)


def children_of(code: str) -> list[str]:
    return [permission.code for permission in CATALOG if permission.parent == code]


def missing_parents(codes) -> dict[str, str]:
    held = set(codes)
    return {code: PARENTS[code] for code in ordered(held) if code in PARENTS and PARENTS[code] not in held}


def describe(codes) -> str:
    return ", ".join(f"'{NAMES.get(code, code)}'" for code in ordered(codes))


def denied_message(codes) -> str:
    if len(codes) == 1:
        return f"You need the {describe(codes)} permission to do this."
    return f"You need one of these permissions to do this: {describe(codes)}."


def missing_parent_message(missing: dict[str, str]) -> str:
    pairs = "; ".join(f"'{NAMES[child]}' needs '{NAMES[parent]}'" for child, parent in missing.items())
    return f"Some permissions need another permission turned on first: {pairs}."
