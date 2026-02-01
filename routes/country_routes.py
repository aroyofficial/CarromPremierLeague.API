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
from core.response import ApiResponse


router = APIRouter(
    prefix="/countries",
    tags=["Countries"]
)


def get_controller(db=Depends(get_db)) -> CountryController:
    repository = CountryRepository(db)
    service = CountryService(repository)
    return CountryController(service)


@router.get("/", response_model=ApiResponse[List[CountryResponse]])
def get_all(controller: CountryController = Depends(get_controller)):
    result = controller.get_all()

    return ApiResponse(
        success=True,
        message="Countries fetched successfully",
        data=result
    )


@router.get("/{country_id}", response_model=ApiResponse[CountryResponse])
def get_by_id(
    country_id: int,
    controller: CountryController = Depends(get_controller)
):
    result = controller.get_by_id(country_id)

    return ApiResponse(
        success=True,
        message="Country fetched successfully",
        data=result
    )


@router.post("/", response_model=ApiResponse[CountryResponse])
def create(
    request: CountryCreateRequest,
    controller: CountryController = Depends(get_controller)
):
    result = controller.create(request)

    return ApiResponse(
        success=True,
        message="Country created successfully",
        data=result
    )


@router.patch("/{country_id}", response_model=ApiResponse[CountryResponse])
def update(
    country_id: int,
    request: CountryUpdateRequest,
    controller: CountryController = Depends(get_controller)
):
    result = controller.update(country_id, request)

    return ApiResponse(
        success=True,
        message="Country updated successfully",
        data=result
    )


@router.delete("/{country_id}", response_model=ApiResponse[bool])
def delete(
    country_id: int,
    controller: CountryController = Depends(get_controller)
):
    result = controller.delete(country_id)

    return ApiResponse(
        success=True,
        message="Country deleted successfully",
        data=result
    )
