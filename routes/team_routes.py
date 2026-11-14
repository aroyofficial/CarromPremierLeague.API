from fastapi import APIRouter, Depends, File, UploadFile
from typing import List, Optional

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
from core.storage import MediaStorage, get_storage
from routes.auth_routes import require_permission
from services.media_service import MediaService, read_image


router = APIRouter(
    prefix="/teams",
    tags=["Teams"]
)


def get_controller(db=Depends(get_db)) -> TeamController:
    repository = TeamRepository(db)
    service = TeamService(repository)
    return TeamController(service)


def get_media_service(
    db=Depends(get_db),
    storage: Optional[MediaStorage] = Depends(get_storage),
) -> MediaService:
    return MediaService(storage, team_repository=TeamRepository(db))


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


@router.post("/", response_model=ApiResponse[TeamResponse], dependencies=[Depends(require_permission("teams.create"))])
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


@router.patch("/{team_id}", response_model=ApiResponse[TeamResponse], dependencies=[Depends(require_permission("teams.update"))])
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


@router.put("/{team_id}/logo", response_model=ApiResponse[TeamResponse], dependencies=[Depends(require_permission("teams.update"))])
def upload_logo(
    team_id: int,
    file: UploadFile = File(...),
    media: MediaService = Depends(get_media_service)
):
    result = media.set_team_logo(team_id, read_image(file.file))

    return ApiResponse(
        success=True,
        message="Team logo updated successfully",
        data=result
    )


@router.delete("/{team_id}/logo", response_model=ApiResponse[TeamResponse], dependencies=[Depends(require_permission("teams.update"))])
def remove_logo(
    team_id: int,
    media: MediaService = Depends(get_media_service)
):
    result = media.remove_team_logo(team_id)

    return ApiResponse(
        success=True,
        message="Team logo removed successfully",
        data=result
    )


@router.delete("/{team_id}", response_model=ApiResponse[bool], dependencies=[Depends(require_permission("teams.delete"))])
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
