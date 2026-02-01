from typing import List, Optional
from fastapi import HTTPException
from repositories.country_repository import CountryRepository
from schemas.country_schema import (
    CountryCreateRequest,
    CountryUpdateRequest,
    CountryResponse
)


class CountryService:

    def __init__(self, repository: CountryRepository):
        self.repository = repository

    def get_all(self) -> List[CountryResponse]:
        return self.repository.get_all()

    def get_by_id(self, country_id: int) -> CountryResponse:
        country = self.repository.get_by_id(country_id)
        if not country:
            raise HTTPException(status_code=404, detail="Country not found.")
        return country

    def create(self, request: CountryCreateRequest) -> CountryResponse:
        name = request.name.strip()
        iso2 = request.iso_code2.strip().upper()
        iso3 = request.iso_code3.strip().upper()

        if self.repository.exists_by_name(name):
            raise HTTPException(status_code=400, detail="Country name already exists.")

        if self.repository.exists_by_iso2(iso2):
            raise HTTPException(status_code=400, detail="ISO Code2 already exists.")

        if self.repository.exists_by_iso3(iso3):
            raise HTTPException(status_code=400, detail="ISO Code3 already exists.")

        normalized_request = CountryCreateRequest(
            name=name,
            iso_code2=iso2,
            iso_code3=iso3,
            capital=request.capital.strip() if request.capital else None,
            phone_code=request.phone_code.strip() if request.phone_code else None,
            continent=request.continent.strip() if request.continent else None
        )

        return self.repository.create(normalized_request)

    def update(self, country_id: int, request: CountryUpdateRequest) -> CountryResponse:
        existing = self.repository.get_by_id(country_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Country not found.")

        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return existing

        if "name" in update_data:
            name = update_data["name"].strip()
            if name != existing.name and self.repository.exists_by_name(name):
                raise HTTPException(status_code=400, detail="Country name already exists.")
            update_data["name"] = name

        if "iso_code2" in update_data:
            iso2 = update_data["iso_code2"].strip().upper()
            if iso2 != existing.iso_code2 and self.repository.exists_by_iso2(iso2):
                raise HTTPException(status_code=400, detail="ISO Code2 already exists.")
            update_data["iso_code2"] = iso2

        if "iso_code3" in update_data:
            iso3 = update_data["iso_code3"].strip().upper()
            if iso3 != existing.iso_code3 and self.repository.exists_by_iso3(iso3):
                raise HTTPException(status_code=400, detail="ISO Code3 already exists.")
            update_data["iso_code3"] = iso3

        if "capital" in update_data and update_data["capital"] is not None:
            update_data["capital"] = update_data["capital"].strip()

        if "phone_code" in update_data and update_data["phone_code"] is not None:
            update_data["phone_code"] = update_data["phone_code"].strip()

        if "continent" in update_data and update_data["continent"] is not None:
            update_data["continent"] = update_data["continent"].strip()

        normalized_request = CountryUpdateRequest(**update_data)

        return self.repository.update(country_id, normalized_request)

    def delete(self, country_id: int) -> bool:
        existing = self.repository.get_by_id(country_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Country not found.")

        return self.repository.delete(country_id)

    def validate_country_exists(self, country_id: int):
        if not self.repository.exists_by_id(country_id):
            raise HTTPException(status_code=400, detail="Invalid nationality_id.")