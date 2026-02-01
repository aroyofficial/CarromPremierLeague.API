from fastapi import APIRouter, Depends
from typing import List
from repositories.player_repository import PlayerRepository
from services.player_service import PlayerService
from controllers.player_controller import PlayerController
from repositories.country_repository import CountryRepository
from services.country_service import CountryService
from schemas.player_schema import (
    PlayerCreateRequest,
    PlayerUpdateRequest,
    PlayerResponse
)
from core.database import get_db


router = APIRouter(
    prefix="/players",
    tags=["Players"]
)


def get_controller(db=Depends(get_db)) -> PlayerController:
    player_repository = PlayerRepository(db)
    country_repository = CountryRepository(db)
    country_service = CountryService(country_repository)
    service = PlayerService(player_repository, country_service)
    return PlayerController(service)


@router.get("/", response_model=List[PlayerResponse])
def get_all(controller: PlayerController = Depends(get_controller)):
    return controller.get_all()


@router.get("/{player_id}", response_model=PlayerResponse)
def get_by_id(player_id: int, controller: PlayerController = Depends(get_controller)):
    return controller.get_by_id(player_id)


@router.post("/", response_model=PlayerResponse)
def create(
    request: PlayerCreateRequest,
    controller: PlayerController = Depends(get_controller)
):
    return controller.create(request)


@router.patch("/{player_id}", response_model=PlayerResponse)
def update(
    player_id: int,
    request: PlayerUpdateRequest,
    controller: PlayerController = Depends(get_controller)
):
    return controller.update(player_id, request)


@router.delete("/{player_id}")
def delete(player_id: int, controller: PlayerController = Depends(get_controller)):
    return controller.delete(player_id)