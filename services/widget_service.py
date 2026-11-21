import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from fastapi import HTTPException, status

from repositories.match_repository import MatchRepository
from repositories.roster_repository import RosterRepository
from repositories.season_repository import SeasonRepository
from repositories.team_repository import TeamRepository
from schemas.auth_schema import AdminResponse
from schemas.widget_schema import (
    WidgetCreatedResponse,
    WidgetCreateRequest,
    WidgetListResponse,
    WidgetResponse,
    WidgetUpdateRequest,
)

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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    )


def _validate_expiry(expires_at: datetime | None) -> datetime | None:
    if expires_at is None:
        return None
    normalized = _as_utc(expires_at)
    if normalized <= _utcnow():
        raise HTTPException(status_code=400, detail="Expiration date must be in the future.")
    return normalized.replace(tzinfo=None)


class DbWidgetSnapshotSource:
    """Reads the same data the public GET endpoints return, through the same repositories."""

    def __init__(self, db):
        self.matches = MatchRepository(db)
        self.teams_repo = TeamRepository(db)
        self.rosters = RosterRepository(db)
        self.seasons = SeasonRepository(db)

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
            {"player_id": player.player_id, "first_name": player.first_name, "last_name": player.last_name}
            for player in roster.players
        ]


class WidgetSnapshots:
    def __init__(self, source):
        self.source = source

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
            "started_at": match.get("started_at"),
            "extra_started_at": match.get("extra_started_at"),
            "team1": self._team(teams, match.get("team1")),
            "team2": self._team(teams, match.get("team2")),
        }

    def build(self, widget_type: str, match_id: int | None, season_id: int | None) -> dict:
        if widget_type == "fixtures":
            return self._fixtures(season_id)
        return self._match(widget_type, match_id)

    def _match(self, widget_type: str, match_id: int) -> dict:
        match = self.source.match(match_id)
        if match is None:
            raise HTTPException(status_code=404, detail="Match not found.")
        teams = self.source.teams()
        view = self._match_view(match, teams)
        if widget_type == "toss":
            return {"type": "toss", "match": view}

        rosters: dict[str, set[int]] = {}
        names: dict[int, str] = {}
        for side, team_id in (("team1", match.get("team1")), ("team2", match.get("team2"))):
            rosters[side] = set()
            if team_id is None:
                continue
            for player in self.source.team_roster(match["season_id"], team_id):
                rosters[side].add(player["player_id"])
                names[player["player_id"]] = f"{player['first_name']} {player['last_name']}".strip()

        sides: dict[str, list[dict]] = {"team1": [], "team2": []}
        for row in self.source.stats(match_id):
            side = next((name for name in ("team1", "team2") if row["player_id"] in rosters.get(name, set())), None)
            if side is None:
                continue
            sides[side].append(
                {
                    "player_id": row["player_id"],
                    "name": names.get(row["player_id"], f"Player {row['player_id']}"),
                    **{field: row.get(field) or 0 for field in STAT_FIELDS},
                }
            )
        return {"type": "scorecard", "match": view, "players": sides}

    def _fixtures(self, season_id: int) -> dict:
        season = self.source.season(season_id)
        if season is None:
            raise HTTPException(status_code=404, detail="Season not found.")
        teams = self.source.teams()
        matches = [self._match_view(match, teams) for match in self.source.season_matches(season_id)]
        return {"type": "fixtures", "season": season, "matches": matches}


class WidgetService:
    def __init__(self, repository, source):
        self.repository = repository
        self.source = source
        self.snapshots = WidgetSnapshots(source)

    def list_widgets(self) -> WidgetListResponse:
        return WidgetListResponse(widgets=[_to_response(row) for row in self.repository.list_all()])

    def _check_target(self, widget_type: str, match_id: int | None, season_id: int | None) -> None:
        if widget_type == "fixtures":
            if not self.source.season_exists(season_id):
                raise HTTPException(status_code=400, detail="Season not found.")
        elif not self.source.match_exists(match_id):
            raise HTTPException(status_code=400, detail="Match not found.")

    def create(self, request: WidgetCreateRequest, actor: AdminResponse) -> WidgetCreatedResponse:
        self._check_target(request.widget_type, request.match_id, request.season_id)
        expires_at = _validate_expiry(request.expires_at)
        raw, prefix, digest = generate_token()
        created = self.repository.create(
            name=request.name,
            widget_type=request.widget_type,
            match_id=request.match_id,
            season_id=request.season_id,
            token_prefix=prefix,
            token_hash=digest,
            expires_at=expires_at,
            created_by=actor.id,
        )
        return WidgetCreatedResponse(**_to_response(created).model_dump(), token=raw)

    def update(self, widget_id: int, request: WidgetUpdateRequest) -> WidgetResponse:
        if self.repository.get_by_id(widget_id) is None:
            raise HTTPException(status_code=404, detail="Widget not found.")
        changes = request.model_dump(exclude_unset=True)
        if changes.get("name") is None:
            changes.pop("name", None)
        if changes.get("is_active") is None:
            changes.pop("is_active", None)
        if "expires_at" in changes:
            changes["expires_at"] = _validate_expiry(changes["expires_at"])
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
        return self.snapshots.build(record["widget_type"], record.get("match_id"), record.get("season_id"))
