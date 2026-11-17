from typing import List, Optional
from datetime import date
from fastapi import HTTPException
from core.phone_numbers import PhoneNumberError, PhoneTaken, canonicalize
from repositories.player_repository import PlayerRepository
from services.country_service import CountryService
from schemas.player_schema import (
    PlayerCreateRequest,
    PlayerUpdateRequest,
    PlayerResponse,
    PlayerProfileResponse
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

    def get_profiles(self) -> List[PlayerProfileResponse]:
        return self.repository.get_profiles()

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

        phone = self._phone(request.phone_code, request.phone_number)
        self._require_free_phone(phone["phone_e164"], None)

        normalized_request = PlayerCreateRequest(
            first_name=first_name,
            last_name=last_name,
            date_of_birth=request.date_of_birth,
            nationality_id=request.nationality_id
        )

        try:
            return self.repository.create(normalized_request, phone)
        except PhoneTaken:
            raise HTTPException(status_code=409, detail="That phone number is already used.") from None

    @staticmethod
    def _phone(phone_code: Optional[str], phone_number: Optional[str]) -> dict:
        """Canonical storage values. All three are None when the player has no phone."""
        code = (phone_code or "").strip()
        number = (phone_number or "").strip()
        if not code and not number:
            return {"phone_code": None, "phone_number": None, "phone_e164": None}
        if not code or not number:
            raise HTTPException(status_code=400, detail="Contact number needs both a country code and a number.")
        try:
            canonical_code, national, e164 = canonicalize(code, number)
        except PhoneNumberError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return {"phone_code": canonical_code, "phone_number": national, "phone_e164": e164}

    def _require_free_phone(self, e164: Optional[str], except_player_id: Optional[int]) -> None:
        if not e164:
            return
        owner = self.repository.find_phone(e164)
        if owner and not (owner[0] == "player" and owner[1] == except_player_id):
            raise HTTPException(status_code=409, detail="That phone number is already used.")

    def update(self, player_id: int, request: PlayerUpdateRequest) -> PlayerResponse:
        existing = self.repository.get_by_id(player_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Player not found.")

        update_data = request.model_dump(exclude_unset=True)

        phone = None
        if "phone_code" in update_data or "phone_number" in update_data:
            stored_code, stored_number = self.repository.get_phone(player_id)
            phone_code = update_data.pop("phone_code", stored_code)
            phone_number = update_data.pop("phone_number", stored_number)
            phone = self._phone(phone_code, phone_number)
            self._require_free_phone(phone["phone_e164"], player_id)

        if not update_data and phone is None:
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

        try:
            return self.repository.update(player_id, normalized_request, phone)
        except PhoneTaken:
            raise HTTPException(status_code=409, detail="That phone number is already used.") from None

    def delete(self, player_id: int) -> bool:
        existing = self.repository.get_by_id(player_id)
        if not existing:
            raise HTTPException(status_code=404, detail="Player not found.")
        return self.repository.delete(player_id)
