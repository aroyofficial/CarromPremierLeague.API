from fastapi import APIRouter, Depends
from typing import List

from core.database import get_db
from controllers.season_controller import SeasonController
from schemas.season_schema import (
    SeasonCreateRequest,
    SeasonUpdateRequest,
    SeasonResponse
)
    
router = APIRouter(prefix="/seasons", tags=["Seasons"])


@router.post("/", response_model=SeasonResponse)
def create_season(
    request: SeasonCreateRequest,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    return controller.create(request)


@router.get("/{season_id}", response_model=SeasonResponse)
def get_season(
    season_id: int,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    return controller.get_by_id(season_id)


@router.get("/", response_model=List[SeasonResponse])
def get_all_seasons(
    db=Depends(get_db)
):
    controller = SeasonController(db)
    return controller.get_all()


@router.put("/{season_id}", response_model=SeasonResponse)
def update_season(
    season_id: int,
    request: SeasonUpdateRequest,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    return controller.update(season_id, request)


@router.delete("/{season_id}")
def delete_season(
    season_id: int,
    db=Depends(get_db)
):
    controller = SeasonController(db)
    return controller.delete(season_id)