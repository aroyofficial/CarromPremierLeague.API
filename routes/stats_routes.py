from fastapi import APIRouter, Depends

from core.database import get_db
from core.response import ApiResponse
from repositories.stats_repository import StatsRepository
from services.stats_service import StatsService
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
    players = controller.get_season_top_coin_potters(season_id, limit)
    is_league_stage_completed = controller.is_league_stage_completed(season_id)
    message = "Top coin potters fetched successfully"

    if not players:
        if controller.has_completed_league_matches(season_id):
            message = (
                "No player stats found for completed league-stage matches in this season"
            )
        else:
            message = "No completed league-stage matches found for this season"

    return ApiResponse(
        success=True,
        message=message,
        data=SeasonTopCoinPottersResponse(
            is_league_stage_completed=is_league_stage_completed,
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
        message = "Data is not available for this season"
    elif not result.players:
        message = "No players are listed for this season"
    else:
        message = "Player stats fetched successfully"

    return ApiResponse(
        success=True,
        message=message,
        data=result
    )
