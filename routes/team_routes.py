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


router = APIRouter(
    prefix="/teams",
    tags=["Teams"]
)


def get_controller(db=Depends(get_db)) -> TeamController:
    repository = TeamRepository(db)
    service = TeamService(repository)
    return TeamController(service)


@router.get("/", response_model=List[TeamResponse])
def get_all(controller: TeamController = Depends(get_controller)):
    return controller.get_all()


@router.get("/{team_id}", response_model=TeamResponse)
def get_by_id(team_id: int, controller: TeamController = Depends(get_controller)):
    return controller.get_by_id(team_id)


@router.post("/", response_model=TeamResponse)
def create(
    request: TeamCreateRequest,
    controller: TeamController = Depends(get_controller)
):
    return controller.create(request)


@router.patch("/{team_id}", response_model=TeamResponse)
def update(
    team_id: int,
    request: TeamUpdateRequest,
    controller: TeamController = Depends(get_controller)
):
    return controller.update(team_id, request)


@router.delete("/{team_id}")
def delete(team_id: int, controller: TeamController = Depends(get_controller)):
    return controller.delete(team_id)
