from typing import List
from datetime import date
from fastapi import HTTPException
from repositories.player_repository import PlayerRepository
from services.country_service import CountryService
from schemas.player_schema import (
    PlayerCreateRequest,
    PlayerUpdateRequest,
    PlayerResponse
)


class PlayerService:

    def __init__(
        self,
        repository: PlayerRepository,
        country_service: CountryService
    ):
        self.repository = repository
        self.country_service = country_service

    def get_all(self) -> List[PlayerResponse]:
        return self.repository.get_all()

    def get_by_id(self, player_id: int) -> PlayerResponse:
        player = self.repository.get_by_id(player_id)
        if not player:
            raise HTTPException(status_code=404, detail="Player not found.")
        return player

    def create(self, request: PlayerCreateRequest) -> PlayerResponse:
        first_name = request.first_name.strip()
        last_name = request.last_name.strip()

        if not first_name:
            raise HTTPException(status_code=400, detail="First name is required.")

        if not last_name:
            raise HTTPException(status_code=400, detail="Last name is required.")

        if request.date_of_birth and request.date_of_birth > date.today():
            raise HTTPException(status_code=400, detail="Date of birth cannot be in the future.")

        if request.nationality_id is not None:
            self.country_service.validate_country_exists(request.nationality_id)

        normalized_request = PlayerCreateRequest(
            first_name=first_name,
            last_name=last_name,
            date_of_birth=request.date_of_birth,
            avatar_url=str(request.avatar_url).strip() if request.avatar_url else None,
            nationality_id=request.nationality_id
        )

        return self.repository.create(normalized_request)

    def update(self, player_id: int, request: PlayerUpdateRequest) -> PlayerResponse:
        existing = self.repository.get_by_id(player_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Player not found.")

        update_data = request.model_dump(exclude_unset=True)

        if not update_data:
            return existing

        if "first_name" in update_data:
            first_name = update_data["first_name"].strip()
            if not first_name:
                raise HTTPException(status_code=400, detail="First name cannot be empty.")
            update_data["first_name"] = first_name

        if "last_name" in update_data:
            last_name = update_data["last_name"].strip()
            if not last_name:
                raise HTTPException(status_code=400, detail="Last name cannot be empty.")
            update_data["last_name"] = last_name

        if "date_of_birth" in update_data:
            dob = update_data["date_of_birth"]
            if dob and dob > date.today():
                raise HTTPException(status_code=400, detail="Date of birth cannot be in the future.")

        if "nationality_id" in update_data and update_data["nationality_id"] is not None:
            self.country_service.validate_country_exists(update_data["nationality_id"])

        normalized_request = PlayerUpdateRequest(**update_data)

        return self.repository.update(player_id, normalized_request)

    def delete(self, player_id: int) -> bool:
        existing = self.repository.get_by_id(player_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Player not found.")
        return self.repository.delete(player_id)