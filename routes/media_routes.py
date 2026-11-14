from typing import Optional

from fastapi import APIRouter, Depends

from core.response import ApiResponse
from core.storage import MediaStorage, get_storage
from schemas.media_schema import MediaDefaultsResponse
from services.media_service import MediaService


router = APIRouter(
    prefix="/media",
    tags=["Media"]
)


@router.get("/defaults", response_model=ApiResponse[MediaDefaultsResponse])
def get_defaults(storage: Optional[MediaStorage] = Depends(get_storage)):
    return ApiResponse(
        success=True,
        message="Default pictures fetched successfully",
        data=MediaService(storage).defaults()
    )
