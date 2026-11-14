from fastapi import APIRouter, Depends, File, Form, UploadFile
from typing import List, Optional

from repositories.player_repository import PlayerRepository
from services.player_service import PlayerService
from controllers.player_controller import PlayerController
from repositories.country_repository import CountryRepository
from services.country_service import CountryService
from schemas.player_schema import (
    PlayerCreateRequest,
    PlayerUpdateRequest,
    PlayerResponse,
    PlayerProfileResponse
)
from core.database import get_db
from core.response import ApiResponse
from core.storage import MediaStorage, get_storage
from routes.auth_routes import require_permission
from services.media_service import MediaService


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


def get_media_service(
    db=Depends(get_db),
    storage: Optional[MediaStorage] = Depends(get_storage),
) -> MediaService:
    return MediaService(storage, player_repository=PlayerRepository(db))


@router.get("/", response_model=ApiResponse[List[PlayerResponse]])
def get_all(controller: PlayerController = Depends(get_controller)):
    result = controller.get_all()

    return ApiResponse(
        success=True,
        message="Players fetched successfully",
        data=result
    )


@router.get("/profiles", response_model=ApiResponse[List[PlayerProfileResponse]])
def get_profiles(controller: PlayerController = Depends(get_controller)):
    result = controller.get_profiles()

    return ApiResponse(
        success=True,
        message="Player profiles fetched successfully",
        data=result
    )


@router.get("/{player_id}", response_model=ApiResponse[PlayerResponse])
def get_by_id(
    player_id: int,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.get_by_id(player_id)

    return ApiResponse(
        success=True,
        message="Player fetched successfully",
        data=result
    )


@router.post("/", response_model=ApiResponse[PlayerResponse], dependencies=[Depends(require_permission("players.create"))])
def create(
    request: PlayerCreateRequest,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.create(request)

    return ApiResponse(
        success=True,
        message="Player created successfully",
        data=result
    )


@router.patch("/{player_id}", response_model=ApiResponse[PlayerResponse], dependencies=[Depends(require_permission("players.update"))])
def update(
    player_id: int,
    request: PlayerUpdateRequest,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.update(player_id, request)

    return ApiResponse(
        success=True,
        message="Player updated successfully",
        data=result
    )


@router.put("/{player_id}/avatar", response_model=ApiResponse[PlayerResponse], dependencies=[Depends(require_permission("players.update"))])
def upload_avatar(
    player_id: int,
    file: Optional[UploadFile] = File(None),
    url: Optional[str] = Form(None),
    media: MediaService = Depends(get_media_service)
):
    result = media.apply_player_picture(player_id, file, url)

    return ApiResponse(
        success=True,
        message="Player picture updated successfully",
        data=result
    )


@router.delete("/{player_id}/avatar", response_model=ApiResponse[PlayerResponse], dependencies=[Depends(require_permission("players.update"))])
def remove_avatar(
    player_id: int,
    media: MediaService = Depends(get_media_service)
):
    result = media.remove_player_avatar(player_id)

    return ApiResponse(
        success=True,
        message="Player picture removed successfully",
        data=result
    )


@router.delete("/{player_id}", response_model=ApiResponse[bool], dependencies=[Depends(require_permission("players.delete"))])
def delete(
    player_id: int,
    controller: PlayerController = Depends(get_controller)
):
    result = controller.delete(player_id)

    return ApiResponse(
        success=True,
        message="Player deleted successfully",
        data=result
    )
