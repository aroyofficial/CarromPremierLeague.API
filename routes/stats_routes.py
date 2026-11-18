from fastapi import APIRouter, Depends

from core.database import get_db
from core.response import ApiResponse
from repositories.stats_repository import StatsRepository
from services.stats_service import NO_MATCH_SCHEDULED, StatsService, golden_striker_state
from controllers.stats_controller import StatsController
from schemas.stats_schema import (
    HeadToHeadResponse,
    SeasonPlayerStatsResponse,
    SeasonTopCoinPottersResponse,
)


router = APIRouter(
    prefix="/stats",
    tags=["Statistics"]
)


def get_controller(db=Depends(get_db)) -> StatsController:
    repository = StatsRepository(db)
    service = StatsService(repository)
    return StatsController(service)


@router.get(
    "/head-to-head",
    response_model=ApiResponse[HeadToHeadResponse]
)
def get_head_to_head(
    team1Id: int,
    team2Id: int,
    controller: StatsController = Depends(get_controller)
):
    result = controller.get_head_to_head(team1Id, team2Id)

    return ApiResponse(
        success=True,
        message="Head to head stats fetched successfully",
        data=result
    )


@router.get(
    "/season/{season_id}/top-coin-potters",
    response_model=ApiResponse[SeasonTopCoinPottersResponse]
)
def get_season_top_coin_potters(
    season_id: int,
    limit: int = 3,
    controller: StatsController = Depends(get_controller)
):
    scheduled, played = controller.match_counts(season_id)
    players = controller.get_season_top_coin_potters(season_id, limit) if played else []
    message, show = golden_striker_state(scheduled, played, len(players))
    if not show:
        players = []

    return ApiResponse(
        success=True,
        message=message,
        data=SeasonTopCoinPottersResponse(
            is_league_stage_completed=controller.is_league_stage_completed(season_id) if show else False,
            matches_played=played,
            players=players
        )
    )


@router.get(
    "/season/{season_id}/players",
    response_model=ApiResponse[SeasonPlayerStatsResponse]
)
def get_season_player_stats(
    season_id: int,
    controller: StatsController = Depends(get_controller)
):
    result = controller.get_season_player_stats(season_id)
    if not result.data_available:
        message = NO_MATCH_SCHEDULED
    elif not result.players:
        message = "No players are listed for this season"
    else:
        message = "Player stats fetched successfully"

    return ApiResponse(
        success=True,
        message=message,
        data=result
    )
