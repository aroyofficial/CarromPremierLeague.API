from fastapi import APIRouter, Depends

from core.database import get_db
from core.response import ApiResponse
from repositories.stats_repository import StatsRepository
from services.stats_service import StatsService
from controllers.stats_controller import StatsController
from schemas.stats_schema import HeadToHeadResponse


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
