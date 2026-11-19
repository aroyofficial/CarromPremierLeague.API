"""Striker's tool catalog. Every tool maps onto an existing API endpoint.

Tools are called through the API itself (see executor.py), so the endpoint's own validation and
permission checks apply. `permissions` here only decides which tools a model is shown; holding any one
code is enough, mirroring require_permission. An empty tuple means any signed-in admin.
"""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel

from schemas.auth_schema import AdminCreateRequest, AdminPermissionsUpdate, AdminUpdateRequest
from schemas.country_schema import CountryCreateRequest, CountryUpdateRequest
from schemas.gallery_schema import GalleryBulkRequest, GalleryCoverRequest, GalleryOrderRequest, GalleryUpdateRequest
from schemas.match_schema import MatchCreateRequest, MatchStatsUpsertRequest, MatchUpdateRequest
from schemas.player_schema import PlayerCreateRequest, PlayerUpdateRequest
from schemas.roster_schema import RemovePlayerRequest, RosterAssignRequest
from schemas.season_schema import SeasonCreateRequest, SeasonTeamAddRequest, SeasonUpdateRequest
from schemas.settings_schema import CompetitionSettings
from schemas.team_schema import TeamCreateRequest, TeamUpdateRequest
from services.assistant.schema import integer, merge, model_parameters, obj, string

Mode = Literal["public", "admin"]

_PATH_PARAM = re.compile(r"{(\w+)}")

_ID_HINTS = {
    "season_id": "Season ID. Find it with list_seasons.",
    "team_id": "Team ID. Find it with list_teams.",
    "player_id": "Player ID. Find it with list_players.",
    "match_id": "Match ID. Find it with list_matches.",
    "country_id": "Country ID. Find it with list_countries.",
    "admin_id": "Admin ID. Find it with list_admins.",
    "item_id": "Gallery item ID. Find it with list_admin_gallery.",
}


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    description: str
    method: str
    path: str
    query: dict[str, str] = field(default_factory=dict)
    extra: dict | None = None
    body: type[BaseModel] | None = None
    form: tuple[str, ...] = ()
    permissions: tuple[str, ...] = ()
    write: bool = False
    public: bool = False
    season_guard: str | None = None
    match_guard: bool = False
    prune_hidden: bool = False

    @property
    def path_params(self) -> list[str]:
        return _PATH_PARAM.findall(self.path)

    @property
    def body_fields(self) -> set[str]:
        if self.body is not None:
            return set(self.parameters().get("properties", {})) - set(self.path_params) - set(self.query)
        return set(self.form)

    def parameters(self) -> dict:
        parts = [obj({name: integer(_ID_HINTS.get(name, "ID.")) for name in self.path_params}, self.path_params)]
        if self.extra:
            parts.append(self.extra)
        if self.body is not None:
            parts.append(model_parameters(self.body))
        return merge(*parts)

    def allowed(self, mode: Mode, permissions: set[str] | None = None) -> bool:
        if mode == "public":
            return self.public
        if self.permissions and not (permissions or set()) & set(self.permissions):
            return False
        return True


_SEASON = obj({"season_id": integer(_ID_HINTS["season_id"])}, ["season_id"])
_GALLERY_ANY = ("gallery.create", "gallery.update", "gallery.publish", "gallery.delete")


def _read(name, title, description, path, **kwargs) -> Tool:
    return Tool(name=name, title=title, description=description, method="GET", path=path, **kwargs)


def _write(name, title, description, method, path, permissions, **kwargs) -> Tool:
    return Tool(
        name=name,
        title=title,
        description=description,
        method=method,
        path=path,
        permissions=permissions,
        write=True,
        **kwargs,
    )


PUBLIC_READS: list[Tool] = [
    _read("list_seasons", "Seasons", "List CPL seasons with ID, name, start and end dates.", "/seasons/", public=True),
    _read(
        "get_season_teams",
        "Season teams",
        "Teams taking part in one season.",
        "/seasons/{season_id}/teams",
        public=True,
        season_guard="season_id",
    ),
    _read(
        "get_league_table",
        "League table",
        "Season standings: matches played, wins, draws, points, net points, head-to-head and golden strike wins. "
        "season_status: 1 not started, 2 in progress, 3 completed. winner_id is the champion team when known.",
        "/seasons/{season_id}/league-table",
        public=True,
        season_guard="season_id",
    ),
    _read(
        "get_season_schedule",
        "Schedule",
        "Full season schedule with teams, dates, results, scores, player stats and win probabilities.",
        "/seasons/{season_id}/schedule",
        public=True,
        season_guard="season_id",
    ),
    _read(
        "list_matches",
        "Matches",
        "Matches of a season with team IDs, date, category, status, outcome and order.",
        "/matches",
        query={"season_id": "seasonId"},
        extra=_SEASON,
        public=True,
        season_guard="season_id",
        prune_hidden=True,
    ),
    _read("get_match", "Match", "One match by ID.", "/matches/{match_id}", public=True, match_guard=True),
    _read(
        "get_match_stats",
        "Match scorecard",
        "Per-player scorecard of one match: coins pocketed, fines, strikers pocketed, shots.",
        "/matches/{match_id}/stats",
        public=True,
        match_guard=True,
    ),
    _read("list_teams", "Teams", "All teams with ID, name and slogan.", "/teams/", public=True),
    _read(
        "list_players",
        "Players",
        "All players with ID, name, date of birth, nationality ID, debut season and career totals.",
        "/players/profiles",
        public=True,
    ),
    _read(
        "get_player_history",
        "Player history",
        "Seasons and teams a player has played for.",
        "/rosters/player/{player_id}",
        public=True,
        prune_hidden=True,
    ),
    _read(
        "get_season_rosters",
        "Season rosters",
        "Every team's players in one season.",
        "/rosters/season/{season_id}",
        public=True,
        season_guard="season_id",
    ),
    _read(
        "get_team_roster",
        "Team roster",
        "Players of one team in one season.",
        "/rosters/team/{team_id}",
        query={"season_id": "seasonId"},
        extra=_SEASON,
        public=True,
        season_guard="season_id",
    ),
    _read(
        "get_top_coin_potters",
        "Top coin potters",
        "Top coin potters of a season's completed league matches.",
        "/stats/season/{season_id}/top-coin-potters",
        query={"limit": "limit"},
        extra=obj({"limit": integer("How many players to return (default 3, up to 20).")}),
        public=True,
        season_guard="season_id",
    ),
    _read(
        "get_season_player_stats",
        "Player stats",
        "Per-player statistics for one season.",
        "/stats/season/{season_id}/players",
        public=True,
        season_guard="season_id",
    ),
    _read(
        "get_head_to_head",
        "Head to head",
        "All-time head-to-head record between two teams.",
        "/stats/head-to-head",
        query={"team1_id": "team1Id", "team2_id": "team2Id"},
        extra=obj(
            {"team1_id": integer("First team ID."), "team2_id": integer("Second team ID.")},
            ["team1_id", "team2_id"],
        ),
        public=True,
    ),
    _read(
        "get_competition_settings",
        "Competition rules",
        "Match rules: regulation minutes, extra minutes, whether the queen counts in league games and needs cover.",
        "/settings",
        public=True,
    ),
    _read("list_countries", "Countries", "Countries with ID, name, ISO codes and phone code.", "/countries/", public=True),
    _read(
        "list_gallery_seasons",
        "Gallery",
        "Seasons that have published gallery photos, with photo counts.",
        "/gallery/seasons",
        public=True,
    ),
    _read(
        "list_gallery_photos",
        "Gallery photos",
        "Published gallery photos of a season with captions and dates.",
        "/gallery",
        query={"season_id": "season_id", "limit": "limit"},
        extra=obj(
            {"season_id": integer(_ID_HINTS["season_id"]), "limit": integer("How many photos (default 24).")},
            ["season_id"],
        ),
        public=True,
    ),
]

ADMIN_READS: list[Tool] = [
    _read("get_my_account", "My account", "The signed-in admin's profile and permission codes.", "/auth/me"),
    _read("list_permissions", "Permission catalog", "Every permission code with its area and meaning.", "/permissions"),
    _read(
        "list_admins",
        "Admins",
        "Admin accounts with profile details and permission codes.",
        "/admins",
        permissions=("admins.view", "permissions.manage"),
    ),
    _read(
        "list_admin_gallery",
        "Gallery library",
        "Gallery items including drafts. Filter by season, status and caption search.",
        "/admin/gallery",
        query={"season_id": "season_id", "status": "status", "search": "q", "limit": "limit"},
        extra=obj(
            {
                "season_id": integer("Only items tagged with this season."),
                "status": string("Publish status filter.", ["all", "published", "draft"]),
                "search": string("Caption search text."),
                "limit": integer("How many items (default 48)."),
            }
        ),
        permissions=_GALLERY_ANY,
    ),
    _read(
        "get_next_match_order",
        "Next match order",
        "The order number the next new match of a season should use.",
        "/matches/next-order",
        query={"season_id": "seasonId"},
        extra=_SEASON,
        permissions=("matches.create",),
    ),
]

_URL = obj({"url": string("Public https link to the image.")}, ["url"])

ADMIN_WRITES: list[Tool] = [
    _write("create_season", "Create season", "Create a season.", "POST", "/seasons/", ("seasons.create",), body=SeasonCreateRequest),
    _write(
        "update_season",
        "Update season",
        "Change a season's name, dates, or public visibility (show).",
        "PUT",
        "/seasons/{season_id}",
        ("seasons.update",),
        body=SeasonUpdateRequest,
    ),
    _write("delete_season", "Delete season", "Delete a season.", "DELETE", "/seasons/{season_id}", ("seasons.delete",)),
    _write(
        "add_team_to_season",
        "Add team to season",
        "Add a team to a season.",
        "POST",
        "/seasons/{season_id}/teams",
        ("seasons.teams",),
        body=SeasonTeamAddRequest,
    ),
    _write(
        "remove_team_from_season",
        "Remove team from season",
        "Remove a team from a season.",
        "DELETE",
        "/seasons/{season_id}/teams/{team_id}",
        ("seasons.teams",),
    ),
    _write(
        "schedule_season_matches",
        "Schedule season matches",
        "Build the league for an upcoming season with at least four teams. Matches that have not started are replaced. In-progress and played matches stay.",
        "POST",
        "/seasons/{season_id}/schedule",
        ("matches.create",),
    ),
    _write("create_team", "Create team", "Create a team.", "POST", "/teams/", ("teams.create",), body=TeamCreateRequest),
    _write(
        "update_team",
        "Update team",
        "Change a team's name or slogan.",
        "PATCH",
        "/teams/{team_id}",
        ("teams.update",),
        body=TeamUpdateRequest,
    ),
    _write("delete_team", "Delete team", "Delete a team.", "DELETE", "/teams/{team_id}", ("teams.delete",)),
    _write(
        "set_team_logo_link",
        "Set team logo",
        "Set a team's logo from a public image link.",
        "PUT",
        "/teams/{team_id}/logo",
        ("teams.update",),
        extra=_URL,
        form=("url",),
    ),
    _write("remove_team_logo", "Remove team logo", "Remove a team's logo.", "DELETE", "/teams/{team_id}/logo", ("teams.update",)),
    _write(
        "create_player",
        "Create player",
        "Create a player. nationality_id is a country ID.",
        "POST",
        "/players/",
        ("players.create",),
        body=PlayerCreateRequest,
    ),
    _write(
        "update_player",
        "Update player",
        "Change a player's name, date of birth or nationality (country ID).",
        "PATCH",
        "/players/{player_id}",
        ("players.update",),
        body=PlayerUpdateRequest,
    ),
    _write("delete_player", "Delete player", "Delete a player.", "DELETE", "/players/{player_id}", ("players.delete",)),
    _write(
        "set_player_picture_link",
        "Set player picture",
        "Set a player's picture from a public image link.",
        "PUT",
        "/players/{player_id}/avatar",
        ("players.update",),
        extra=_URL,
        form=("url",),
    ),
    _write(
        "remove_player_picture",
        "Remove player picture",
        "Remove a player's picture.",
        "DELETE",
        "/players/{player_id}/avatar",
        ("players.update",),
    ),
    _write("create_country", "Create country", "Add a country.", "POST", "/countries/", ("countries.create",), body=CountryCreateRequest),
    _write(
        "update_country",
        "Update country",
        "Change a country.",
        "PATCH",
        "/countries/{country_id}",
        ("countries.update",),
        body=CountryUpdateRequest,
    ),
    _write("delete_country", "Delete country", "Delete a country.", "DELETE", "/countries/{country_id}", ("countries.delete",)),
    _write(
        "assign_player_to_team",
        "Assign player",
        "Put a player in a team's roster for a season. A team holds two players.",
        "POST",
        "/rosters/assign",
        ("rosters.manage",),
        body=RosterAssignRequest,
    ),
    _write(
        "remove_player_from_roster",
        "Remove player from roster",
        "Take a player out of their team for a season.",
        "DELETE",
        "/rosters/remove",
        ("rosters.manage",),
        body=RemovePlayerRequest,
    ),
    _write(
        "create_match",
        "Create match",
        "Schedule a match. Use get_next_match_order for order. New matches normally use status 1 and outcome 3.",
        "POST",
        "/matches",
        ("matches.create",),
        body=MatchCreateRequest,
    ),
    _write(
        "update_match",
        "Update match",
        "Change a match: teams, date, duration, status (1 scheduled, 2 in progress, 3 played), "
        "net points, outcome (1 team1 win, 2 team2 win, 3 not decided), toss outcome.",
        "PATCH",
        "/matches/{match_id}",
        ("matches.update",),
        body=MatchUpdateRequest,
    ),
    _write(
        "reset_match",
        "Reset match",
        "Reset a match to scheduled and clear its result.",
        "POST",
        "/matches/{match_id}/reset",
        ("matches.reset",),
    ),
    _write("delete_match", "Delete match", "Delete a match.", "DELETE", "/matches/{match_id}", ("matches.delete",)),
    _write(
        "save_match_scorecard",
        "Save scorecard",
        "Save per-player stats for a match. Players must be on the two teams' rosters.",
        "PUT",
        "/matches/{match_id}/stats",
        ("matches.scorecard",),
        body=MatchStatsUpsertRequest,
    ),
    _write(
        "update_competition_settings",
        "Update competition rules",
        "Replace the competition rules. assistant_public_enabled and assistant_admin_enabled turn the Striker chatbot on or off for the public site and for the admin console. Read get_competition_settings first and send every field.",
        "PUT",
        "/settings",
        ("settings.update",),
        body=CompetitionSettings,
    ),
    _write(
        "update_gallery_photo",
        "Update gallery item",
        "Change a gallery item's caption, alt text, date, publish state or season tags.",
        "PATCH",
        "/gallery/{item_id}",
        ("gallery.update",),
        body=GalleryUpdateRequest,
    ),
    _write(
        "bulk_update_gallery",
        "Bulk gallery change",
        "Publish, hide, tag with seasons, or delete several gallery items at once.",
        "POST",
        "/gallery/bulk",
        ("gallery.update",),
        body=GalleryBulkRequest,
    ),
    _write(
        "delete_gallery_photo",
        "Delete gallery item",
        "Delete a gallery item.",
        "DELETE",
        "/gallery/{item_id}",
        ("gallery.delete",),
    ),
    _write(
        "set_season_gallery_cover",
        "Set gallery cover",
        "Choose the cover photo of a season's gallery.",
        "PUT",
        "/seasons/{season_id}/gallery-cover",
        ("gallery.update",),
        body=GalleryCoverRequest,
    ),
    _write(
        "reorder_gallery",
        "Reorder gallery",
        "Set gallery order. item_ids lists items first to last; season_id orders that season, otherwise the library.",
        "PUT",
        "/gallery/order",
        ("gallery.update",),
        body=GalleryOrderRequest,
    ),
    _write(
        "add_gallery_link",
        "Add gallery link",
        "Add a gallery draft from a public image or video link.",
        "POST",
        "/gallery",
        ("gallery.create",),
        extra=obj(
            {
                "url": string("Public https link to the image or video."),
                "season_ids": string("Comma-separated season IDs to tag."),
                "caption": string("Caption."),
                "alt_text": string("Alt text."),
                "taken_on": string("Date and time as YYYY-MM-DDTHH:mm:ss. A date alone is midnight."),
            },
            ["url"],
        ),
        form=("url", "season_ids", "caption", "alt_text", "taken_on"),
    ),
    _write(
        "create_admin",
        "Create admin",
        "Create an inactive admin. Email and contact number are required. A temporary password is generated and emailed; do not send a password.",
        "POST",
        "/admins",
        ("admins.create",),
        body=AdminCreateRequest,
    ),
    _write(
        "update_admin",
        "Update admin",
        "Change an admin's profile, username, password or active state. Admins may always edit themselves.",
        "PATCH",
        "/admins/{admin_id}",
        (),
        body=AdminUpdateRequest,
    ),
    _write("delete_admin", "Delete admin", "Delete an admin account.", "DELETE", "/admins/{admin_id}", ("admins.delete",)),
    _write(
        "set_admin_permissions",
        "Set admin permissions",
        "Replace an admin's permission codes. Use list_permissions for valid codes.",
        "PUT",
        "/admins/{admin_id}/permissions",
        ("permissions.manage",),
        body=AdminPermissionsUpdate,
    ),
]

CATALOG: dict[str, Tool] = {tool.name: tool for tool in PUBLIC_READS + ADMIN_READS + ADMIN_WRITES}


def tools_for(mode: Mode, permissions: set[str] | None = None) -> list[Tool]:
    return [tool for tool in CATALOG.values() if tool.allowed(mode, permissions)]
