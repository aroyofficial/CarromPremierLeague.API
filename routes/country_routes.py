from fastapi import APIRouter, Depends
from typing import List
from repositories.country_repository import CountryRepository
from services.country_service import CountryService
from controllers.country_controller import CountryController
from schemas.country_schema import (
    CountryCreateRequest,
    CountryUpdateRequest,
    CountryResponse
)
from core.database import get_db


router = APIRouter(
    prefix="/countries",
    tags=["Countries"]
)


def get_controller(db=Depends(get_db)) -> CountryController:
    repository = CountryRepository(db)
    service = CountryService(repository)
    return CountryController(service)


@router.get("/", response_model=List[CountryResponse])
def get_all(controller: CountryController = Depends(get_controller)):
    return controller.get_all()


@router.get("/{country_id}", response_model=CountryResponse)
def get_by_id(country_id: int, controller: CountryController = Depends(get_controller)):
    return controller.get_by_id(country_id)


@router.post("/", response_model=CountryResponse)
def create(
    request: CountryCreateRequest,
    controller: CountryController = Depends(get_controller)
):
    return controller.create(request)


@router.patch("/{country_id}", response_model=CountryResponse)
def update(
    country_id: int,
    request: CountryUpdateRequest,
    controller: CountryController = Depends(get_controller)
):
    return controller.update(country_id, request)


@router.delete("/{country_id}")
def delete(country_id: int, controller: CountryController = Depends(get_controller)):
    return controller.delete(country_id)