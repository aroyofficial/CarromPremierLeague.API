from schemas.settings_schema import CompetitionSettings
from services.settings_service import SettingsService


class SettingsController:

    def __init__(self, service: SettingsService):
        self.service = service

    def get(self) -> CompetitionSettings:
        return self.service.get()

    def update(self, request: CompetitionSettings) -> CompetitionSettings:
        return self.service.update(request)
