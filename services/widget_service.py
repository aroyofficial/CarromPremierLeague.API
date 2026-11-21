import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException

from repositories.match_repository import MatchRepository
from repositories.roster_repository import RosterRepository
from repositories.season_repository import SeasonRepository
from repositories.settings_repository import SettingsRepository
from repositories.stats_repository import StatsRepository
from repositories.team_repository import TeamRepository
from schemas.auth_schema import AdminResponse
from schemas.widget_schema import (
    SEASON_SCOPED_TYPES,
    WidgetCreatedResponse,
    WidgetCreateRequest,
    WidgetFlowConfig,
    WidgetListResponse,
    WidgetResponse,
    WidgetUpdateRequest,
)
from services.season_service import SeasonService
from services.stats_service import StatsService

TOKEN_PREFIX = "cpl_widget_"
INACTIVE_MESSAGE = "This widget link is not active."
STAT_FIELDS = (
    "coins_pocketed",
    "opponent_coins_pocketed",
    "single_fines",
    "double_fines",
    "strikers_pocketed",
    "coins_fined",
    "shots_taken",
)
DEFAULT_CLOCK = {
    "regulation_minutes": 15,
    "extra_minutes": 5,
    "league_queen": False,
    "queen_needs_cover": False,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(now: datetime | None = None) -> str:
    return (now or _utcnow()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    raw = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    return raw, raw[:18], hash_token(raw)


def widget_status(record: dict, now: datetime | None = None) -> str:
    if not record.get("is_active"):
        return "revoked"
    expires_at = _as_utc(record.get("expires_at"))
    if expires_at is not None and expires_at <= (now or _utcnow()):
        return "expired"
    return "active"


def _coerce_flow(raw_flow: dict | None, widget_type: str) -> WidgetFlowConfig | None:
    if widget_type != "season_flow":
        return None
    if not raw_flow:
        return WidgetFlowConfig()
    try:
        return WidgetFlowConfig.model_validate(raw_flow)
    except Exception:
        return WidgetFlowConfig()


def _to_response(record: dict) -> WidgetResponse:
    return WidgetResponse(
        id=record["id"],
        name=record["name"],
        widget_type=record["widget_type"],
        match_id=record.get("match_id"),
        season_id=record.get("season_id"),
        token_prefix=record["token_prefix"],
        is_active=bool(record["is_active"]),
        status=widget_status(record),
        expires_at=record.get("expires_at"),
        last_used_at=record.get("last_used_at"),
        revoked_at=record.get("revoked_at"),
        created_at=record.get("created_at"),
        updated_at=record.get("updated_at"),
        created_by=record["created_by"],
        flow_config=_coerce_flow(record.get("flow_config"), record["widget_type"]),
    )


def _validate_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    normalized = _as_utc(expires_at)
    if normalized <= _utcnow():
        raise HTTPException(status_code=400, detail="Expiration date must be in the future.")
    return normalized.replace(tzinfo=None)


class DbWidgetSnapshotSource:
    """Reads the same data the public GET endpoints return, through the same repositories and services."""

    def __init__(self, db):
        self.matches = MatchRepository(db)
        self.teams_repo = TeamRepository(db)
        self.rosters = RosterRepository(db)
        self.seasons = SeasonRepository(db)
        self.season_service = SeasonService(self.seasons)
        self.stats_service = StatsService(StatsRepository(db))
        self.settings = SettingsRepository(db)

    def match_exists(self, match_id: int) -> bool:
        return self.matches.get_by_id(match_id) is not None

    def season_exists(self, season_id: int) -> bool:
        return self.seasons.get_by_id(season_id) is not None

    def match(self, match_id: int) -> dict | None:
        found = self.matches.get_by_id(match_id)
        return found.model_dump(mode="json") if found else None

    def stats(self, match_id: int) -> list[dict]:
        return [row.model_dump(mode="json") for row in self.matches.get_stats(match_id)]

    def season(self, season_id: int) -> dict | None:
        found = self.seasons.get_by_id(season_id)
        return {"id": found.id, "name": found.name} if found else None

    def season_matches(self, season_id: int) -> list[dict]:
        return [row.model_dump(mode="json") for row in self.matches.get_all(season_id=season_id)]

    def teams(self) -> dict[int, dict]:
        return {team.id: {"name": team.name, "logo_url": team.logo_url} for team in self.teams_repo.get_all()}

    def team_roster(self, season_id: int, team_id: int) -> list[dict]:
        roster = self.rosters.get_team_roster(season_id, team_id)
        return [
            {
                "player_id": player.player_id,
                "first_name": player.first_name,
                "last_name": player.last_name,
                "avatar_url": getattr(player, "avatar_url", None),
            }
            for player in roster.players
        ]

    def league_table(self, season_id: int) -> dict:
        return self.season_service.get_league_table(season_id).model_dump(mode="json")

    def top_stats(self, season_id: int, limit: int = 5) -> dict:
        scheduled, played = self.stats_service.match_counts(season_id)
        players = (
            [row.model_dump(mode="json") for row in self.stats_service.get_season_top_coin_potters(season_id, limit)]
            if played
            else []
        )
        completed = bool(self.stats_service.is_league_stage_completed(season_id)) if played and players else False
        return {
            "matches_scheduled": int(scheduled or 0),
            "matches_played": int(played or 0),
            "is_league_stage_completed": completed,
            "players": players,
        }

    def head_to_head(self, team1_id: int | None, team2_id: int | None) -> dict | None:
        if not team1_id or not team2_id or team1_id == team2_id:
            return None
        return self.stats_service.get_head_to_head(team1_id, team2_id).model_dump(mode="json")

    def clock_settings(self) -> dict:
        clock = self.settings.get()
        return {
            "regulation_minutes": clock.regulation_minutes,
            "extra_minutes": clock.extra_minutes,
            "league_queen": clock.league_queen,
            "queen_needs_cover": clock.queen_needs_cover,
        }


class WidgetSnapshots:
    def __init__(self, source):
        self.source = source

    def _clock(self) -> dict:
        getter = getattr(self.source, "clock_settings", None)
        return getter() if callable(getter) else dict(DEFAULT_CLOCK)

    def _team(self, teams: dict[int, dict], team_id: int | None) -> dict | None:
        if team_id is None:
            return None
        team = teams.get(team_id, {})
        return {"id": team_id, "name": team.get("name") or "TBD", "logo_url": team.get("logo_url")}

    def _match_view(self, match: dict, teams: dict[int, dict]) -> dict:
        return {
            "id": match["id"],
            "season_id": match["season_id"],
            "order": match["order"],
            "scheduled_date": match["scheduled_date"],
            "category": match["category"],
            "status": match["status"],
            "outcome": match["outcome"],
            "net_points": match["net_points"],
            "duration": match["duration"],
            "extra": match["extra"],
            "golden_strike": match["golden_strike"],
            "toss_outcome": match["toss_outcome"],
            "ready_at": match.get("ready_at"),
            "started_at": match.get("started_at"),
            "extra_started_at": match.get("extra_started_at"),
            "completed_at": match.get("completed_at"),
            "team1": self._team(teams, match.get("team1")),
            "team2": self._team(teams, match.get("team2")),
        }

    def _rosters_and_players(self, match: dict) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
        stats_by_player = {int(row["player_id"]): row for row in self.source.stats(int(match["id"]))}
        rosters_view: dict[str, list[dict]] = {"team1": [], "team2": []}
        players_view: dict[str, list[dict]] = {"team1": [], "team2": []}

        for side, team_id in (("team1", match.get("team1")), ("team2", match.get("team2"))):
            if team_id is None:
                continue
            for player in self.source.team_roster(match["season_id"], team_id):
                pid = int(player["player_id"])
                full_name = f"{player['first_name']} {player['last_name']}".strip()
                avatar_url = player.get("avatar_url")
                rosters_view[side].append(
                    {"player_id": pid, "name": full_name, "avatar_url": avatar_url}
                )
                stat = stats_by_player.get(pid, {})
                players_view[side].append(
                    {
                        "player_id": pid,
                        "name": full_name,
                        "avatar_url": avatar_url,
                        **{field: stat.get(field) or 0 for field in STAT_FIELDS},
                    }
                )
        return rosters_view, players_view

    def _head_to_head(self, match: dict) -> dict | None:
        getter = getattr(self.source, "head_to_head", None)
        if not callable(getter):
            return None
        return getter(match.get("team1"), match.get("team2"))

    def _match_bundle(self, match: dict, teams: dict[int, dict]) -> dict:
        view = self._match_view(match, teams)
        rosters_view, players_view = self._rosters_and_players(match)
        return {
            "match": view,
            "rosters": rosters_view,
            "players": players_view,
            "head_to_head": self._head_to_head(match),
        }

    def _enriched_points_table(self, season_id: int, teams: dict[int, dict]) -> dict:
        getter = getattr(self.source, "league_table", None)
        raw = getter(season_id) if callable(getter) else {"winner_id": None, "season_status": 1, "data_available": True, "standings": []}
        standings = []
        for idx, row in enumerate(raw.get("standings") or [], start=1):
            team_id = row.get("team_id")
            team_info = teams.get(int(team_id), {}) if team_id is not None else {}
            standings.append(
                {
                    **row,
                    "rank": idx,
                    "logo_url": team_info.get("logo_url"),
                }
            )
        winner_id = raw.get("winner_id")
        winner_name = teams.get(int(winner_id), {}).get("name") if winner_id is not None else None
        return {
            "winner_id": winner_id,
            "winner_name": winner_name,
            "season_status": raw.get("season_status", 1),
            "data_available": bool(raw.get("data_available", True)),
            "standings": standings,
        }

    def _enriched_top_stats(self, season_id: int, teams: dict[int, dict]) -> dict:
        getter = getattr(self.source, "top_stats", None)
        raw = (
            getter(season_id, 5)
            if callable(getter)
            else {"matches_scheduled": 0, "matches_played": 0, "is_league_stage_completed": False, "players": []}
        )
        players = []
        for idx, player in enumerate(raw.get("players") or [], start=1):
            team_id = player.get("team_id")
            team_info = teams.get(int(team_id), {}) if team_id is not None else {}
            players.append(
                {
                    **player,
                    "rank": idx,
                    "name": f"{player.get('first_name', '')} {player.get('last_name', '')}".strip(),
                    "team_logo_url": team_info.get("logo_url"),
                }
            )
        return {
            "matches_scheduled": raw.get("matches_scheduled", 0),
            "matches_played": raw.get("matches_played", 0),
            "is_league_stage_completed": bool(raw.get("is_league_stage_completed", False)),
            "players": players,
        }

    def build(
        self,
        widget_type: str,
        match_id: int | None,
        season_id: int | None,
        flow_config: WidgetFlowConfig | None = None,
    ) -> dict:
        if widget_type == "season_flow":
            return self._season_flow(season_id, flow_config or WidgetFlowConfig())
        if widget_type == "fixtures":
            return self._fixtures(season_id)
        if widget_type == "points_table":
            return self._points_table(season_id)
        if widget_type == "stats":
            return self._stats(season_id)
        return self._match(widget_type, match_id)

    def _match(self, widget_type: str, match_id: int) -> dict:
        match = self.source.match(match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="Match not found.")
        teams = self.source.teams()
        view = self._match_view(match, teams)
        rosters_view, players_view = self._rosters_and_players(match)
        if widget_type == "toss":
            return {
                "type": "toss",
                "server_now": _utc_iso(),
                "match": view,
                "rosters": rosters_view,
                "head_to_head": self._head_to_head(match),
            }
        return {
            "type": "scorecard",
            "server_now": _utc_iso(),
            "clock": self._clock(),
            "match": view,
            "players": players_view,
        }

    def _require_season(self, season_id: int) -> dict:
        season = self.source.season(season_id)
        if season is None:
            raise HTTPException(status_code=404, detail="Season not found.")
        return season

    def _fixtures(self, season_id: int) -> dict:
        season = self._require_season(season_id)
        teams = self.source.teams()
        matches = [self._match_view(match, teams) for match in self.source.season_matches(season_id)]
        return {"type": "fixtures", "server_now": _utc_iso(), "season": season, "matches": matches}

    def _points_table(self, season_id: int) -> dict:
        season = self._require_season(season_id)
        teams = self.source.teams()
        return {
            "type": "points_table",
            "server_now": _utc_iso(),
            "season": season,
            "points_table": self._enriched_points_table(season_id, teams),
        }

    def _stats(self, season_id: int) -> dict:
        season = self._require_season(season_id)
        teams = self.source.teams()
        return {
            "type": "stats",
            "server_now": _utc_iso(),
            "season": season,
            "top_stats": self._enriched_top_stats(season_id, teams),
        }

    def _season_flow(self, season_id: int, flow_config: WidgetFlowConfig) -> dict:
        season = self._require_season(season_id)
        teams = self.source.teams()
        raw_matches = sorted(self.source.season_matches(season_id), key=lambda m: (m.get("order") or 0, m["id"]))
        matches_view = [self._match_view(m, teams) for m in raw_matches]
        by_id = {int(m["id"]): m for m in raw_matches}

        live_candidates = [m for m in raw_matches if m.get("status") == 2]
        live_raw = (
            sorted(live_candidates, key=lambda m: (m.get("started_at") or "", m.get("order") or 0, m["id"]))[-1]
            if live_candidates
            else None
        )

        pre_raw = None
        if live_raw is None:
            scheduled = [m for m in raw_matches if m.get("status") == 1]
            tossed = [m for m in scheduled if m.get("toss_outcome") in (1, 2)]
            ready = [m for m in scheduled if m.get("ready_at")]
            if tossed:
                ready_tossed = [m for m in tossed if m.get("ready_at")]
                pre_raw = (ready_tossed or tossed)[0]
            elif ready:
                pre_raw = sorted(ready, key=lambda m: (m.get("ready_at") or "", m.get("order") or 0))[-1]

        active_raw = live_raw or pre_raw

        played_candidates = [m for m in raw_matches if m.get("status") == 3]
        last_completed_raw = (
            sorted(played_candidates, key=lambda m: (m.get("completed_at") or "", m.get("order") or 0, m["id"]))[-1]
            if played_candidates
            else None
        )

        upcoming_candidates = [m for m in raw_matches if m.get("status") == 1]
        next_scheduled_raw = upcoming_candidates[0] if upcoming_candidates else None

        bundle_cache: dict[int, dict] = {}

        def bundle_for(raw: dict | None) -> dict | None:
            if raw is None:
                return None
            mid = int(raw["id"])
            if mid not in bundle_cache:
                bundle_cache[mid] = self._match_bundle(raw, teams)
            return bundle_cache[mid]

        pinned_raw = None
        if flow_config.director.mode == "pin" and flow_config.director.match_id is not None:
            pinned_raw = by_id.get(int(flow_config.director.match_id))

        return {
            "type": "season_flow",
            "server_now": _utc_iso(),
            "season": season,
            "clock": self._clock(),
            "flow": flow_config.model_dump(mode="json"),
            "matches": matches_view,
            "active_match": bundle_for(active_raw),
            "last_completed_match": bundle_for(last_completed_raw),
            "next_scheduled_match": bundle_for(next_scheduled_raw),
            "pinned_match": bundle_for(pinned_raw),
            "points_table": self._enriched_points_table(season_id, teams),
            "top_stats": self._enriched_top_stats(season_id, teams),
        }


class WidgetService:
    def __init__(self, repository, source):
        self.repository = repository
        self.source = source
        self.snapshots = WidgetSnapshots(source)

    def list_widgets(self) -> WidgetListResponse:
        return WidgetListResponse(widgets=[_to_response(row) for row in self.repository.list_all()])

    def _check_target(self, widget_type: str, match_id: int | None, season_id: int | None) -> None:
        if widget_type in SEASON_SCOPED_TYPES:
            if not self.source.season_exists(season_id):
                raise HTTPException(status_code=400, detail="Season not found.")
        elif not self.source.match_exists(match_id):
            raise HTTPException(status_code=400, detail="Match not found.")

    def create(self, request: WidgetCreateRequest, actor: AdminResponse) -> WidgetCreatedResponse:
        self._check_target(request.widget_type, request.match_id, request.season_id)
        expires_at = _validate_expiry(request.expires_at)
        raw, prefix, digest = generate_token()
        flow_dump = request.flow_config.model_dump(mode="json") if request.flow_config is not None else None
        created = self.repository.create(
            name=request.name,
            widget_type=request.widget_type,
            match_id=request.match_id,
            season_id=request.season_id,
            token_prefix=prefix,
            token_hash=digest,
            expires_at=expires_at,
            created_by=actor.id,
            flow_config=flow_dump,
        )
        return WidgetCreatedResponse(**_to_response(created).model_dump(), token=raw)

    def update(self, widget_id: int, request: WidgetUpdateRequest) -> WidgetResponse:
        existing = self.repository.get_by_id(widget_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Widget not found.")
        changes = request.model_dump(exclude_unset=True)
        if changes.get("name") is None:
            changes.pop("name", None)
        if changes.get("is_active") is None:
            changes.pop("is_active", None)
        if "expires_at" in changes:
            changes["expires_at"] = _validate_expiry(changes["expires_at"])

        director_override = changes.pop("director", None)
        if "flow_config" in changes:
            if existing["widget_type"] != "season_flow":
                raise HTTPException(status_code=400, detail="Only a season flow widget carries a flow configuration.")
            flow = request.flow_config or WidgetFlowConfig()
            if request.director is not None:
                flow.director = request.director
            changes["flow_config"] = flow.model_dump(mode="json")
        elif director_override is not None and request.director is not None:
            if existing["widget_type"] != "season_flow":
                raise HTTPException(status_code=400, detail="Only a season flow widget supports Live Director overrides.")
            current_flow = _coerce_flow(existing.get("flow_config"), "season_flow") or WidgetFlowConfig()
            current_flow.director = request.director
            changes["flow_config"] = current_flow.model_dump(mode="json")

        updated = self.repository.update(widget_id, changes)
        if updated is None:
            raise HTTPException(status_code=404, detail="Widget not found.")
        return _to_response(updated)

    def rotate(self, widget_id: int) -> WidgetCreatedResponse:
        if self.repository.get_by_id(widget_id) is None:
            raise HTTPException(status_code=404, detail="Widget not found.")
        raw, prefix, digest = generate_token()
        rotated = self.repository.rotate(widget_id, prefix, digest)
        if rotated is None:
            raise HTTPException(status_code=404, detail="Widget not found.")
        return WidgetCreatedResponse(**_to_response(rotated).model_dump(), token=raw)

    def preview_widget(self, widget_id: int) -> dict:
        record = self.repository.get_by_id(widget_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Widget not found.")
        flow = _coerce_flow(record.get("flow_config"), record["widget_type"])
        return self.snapshots.build(record["widget_type"], record.get("match_id"), record.get("season_id"), flow)

    def preview_season(self, season_id: int) -> dict:
        if not self.source.season_exists(season_id):
            raise HTTPException(status_code=404, detail="Season not found.")
        return self.snapshots.build("season_flow", None, season_id, WidgetFlowConfig())

    def _authorize(self, raw_token: str) -> dict:
        token = (raw_token or "").strip()
        if not token.startswith(TOKEN_PREFIX) or len(token) < len(TOKEN_PREFIX) + 16:
            raise HTTPException(status_code=401, detail=INACTIVE_MESSAGE)
        digest = hash_token(token)
        record = self.repository.get_by_token_hash(digest)
        if not record or not hmac.compare_digest(record["token_hash"], digest):
            raise HTTPException(status_code=401, detail=INACTIVE_MESSAGE)
        if widget_status(record) != "active":
            raise HTTPException(status_code=401, detail=INACTIVE_MESSAGE)
        self.repository.touch_last_used(int(record["id"]))
        return record

    def snapshot(self, raw_token: str) -> dict:
        record = self._authorize(raw_token)
        flow = _coerce_flow(record.get("flow_config"), record["widget_type"])
        return self.snapshots.build(record["widget_type"], record.get("match_id"), record.get("season_id"), flow)
