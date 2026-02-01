from fastapi import APIRouter, Depends
from typing import List

from schemas.team_schema import (
    TeamCreateRequest,
    TeamUpdateRequest,
    TeamResponse
)
from services.team_service import TeamService
from repositories.team_repository import TeamRepository
from controllers.team_controller import TeamController
from core.database import get_db
from core.response import ApiResponse


router = APIRouter(
    prefix="/teams",
    tags=["Teams"]
)


def get_controller(db=Depends(get_db)) -> TeamController:
    repository = TeamRepository(db)
    service = TeamService(repository)
    return TeamController(service)


@router.get("/", response_model=ApiResponse[List[TeamResponse]])
def get_all(controller: TeamController = Depends(get_controller)):
    result = controller.get_all()

    return ApiResponse(
        success=True,
        message="Teams fetched successfully",
        data=result
    )


@router.get("/{team_id}", response_model=ApiResponse[TeamResponse])
def get_by_id(
    team_id: int,
    controller: TeamController = Depends(get_controller)
):
    result = controller.get_by_id(team_id)

    return ApiResponse(
        success=True,
        message="Team fetched successfully",
        data=result
    )


@router.post("/", response_model=ApiResponse[TeamResponse])
def create(
    request: TeamCreateRequest,
    controller: TeamController = Depends(get_controller)
):
    result = controller.create(request)

    return ApiResponse(
        success=True,
        message="Team created successfully",
        data=result
    )


@router.patch("/{team_id}", response_model=ApiResponse[TeamResponse])
def update(
    team_id: int,
    request: TeamUpdateRequest,
    controller: TeamController = Depends(get_controller)
):
    result = controller.update(team_id, request)

    return ApiResponse(
        success=True,
        message="Team updated successfully",
        data=result
    )


@router.delete("/{team_id}", response_model=ApiResponse[bool])
def delete(
    team_id: int,
    controller: TeamController = Depends(get_controller)
):
    result = controller.delete(team_id)

    return ApiResponse(
        success=True,
        message="Team deleted successfully",
        data=result
    )
