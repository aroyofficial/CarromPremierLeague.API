from typing import List
from services.country_service import CountryService
from schemas.country_schema import (
    CountryCreateRequest,
    CountryUpdateRequest,
    CountryResponse
)


class CountryController:

    def __init__(self, service: CountryService):
        self.service = service

    def get_all(self) -> List[CountryResponse]:
        return self.service.get_all()

    def get_by_id(self, country_id: int) -> CountryResponse:
        return self.service.get_by_id(country_id)

    def create(self, request: CountryCreateRequest) -> CountryResponse:
        return self.service.create(request)

    def update(self, country_id: int, request: CountryUpdateRequest) -> CountryResponse:
        return self.service.update(country_id, request)

    def delete(self, country_id: int) -> dict:
        self.service.delete(country_id)
        return {"message": "Country deleted successfully"}