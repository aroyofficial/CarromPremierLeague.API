from fastapi import APIRouter, Depends

from controllers.settings_controller import SettingsController
from core.database import get_db
from core.response import ApiResponse
from repositories.settings_repository import SettingsRepository
from routes.auth_routes import get_current_admin
from schemas.settings_schema import CompetitionSettings
from services.settings_service import SettingsService

router = APIRouter(prefix="/settings", tags=["Settings"])


def get_controller(db=Depends(get_db)) -> SettingsController:
    return SettingsController(SettingsService(SettingsRepository(db)))


@router.get("", response_model=ApiResponse[CompetitionSettings])
def get_settings(controller: SettingsController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Competition settings fetched successfully",
        data=controller.get(),
    )


@router.put("", response_model=ApiResponse[CompetitionSettings], dependencies=[Depends(get_current_admin)])
def update_settings(
    request: CompetitionSettings,
    controller: SettingsController = Depends(get_controller),
):
    return ApiResponse(
        success=True,
        message="Competition settings saved successfully",
        data=controller.update(request),
    )
