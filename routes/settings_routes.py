from fastapi import APIRouter, Depends

from controllers.settings_controller import SettingsController
from core.database import get_db
from core.response import ApiResponse
from repositories.settings_repository import SettingsRepository
from routes.auth_routes import require_permission
from schemas.settings_schema import CompetitionSettings
from services.settings_service import SettingsService
from services.webhooks.enqueue import publish_webhook_event

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


@router.put("", response_model=ApiResponse[CompetitionSettings], dependencies=[Depends(require_permission("settings.update"))])
def update_settings(
    request: CompetitionSettings,
    controller: SettingsController = Depends(get_controller),
):
    data = controller.update(request)
    db = getattr(getattr(getattr(controller, "service", None), "repository", None), "db", None)
    publish_webhook_event(db, "settings.updated", data)
    return ApiResponse(
        success=True,
        message="Competition settings saved successfully",
        data=data,
    )
