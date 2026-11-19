from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile
from fastapi.responses import Response as RawResponse

from core.database import get_db
from core.response import ApiResponse
from core.storage import get_storage
from repositories.gallery_repository import GalleryRepository
from routes.auth_routes import require_permission
from schemas.auth_schema import AdminResponse
from schemas.gallery_schema import (
    GalleryBulkRequest,
    GalleryBulkResult,
    GalleryCoverRequest,
    GalleryOrderRequest,
    GalleryPage,
    GalleryPhoto,
    GallerySeason,
    GalleryUpdateRequest,
    parse_taken_on,
)
from services.gallery_service import GalleryService

router = APIRouter(tags=["Gallery"])

_ANY_GALLERY = ("gallery.create", "gallery.update", "gallery.publish", "gallery.delete")
_PUBLIC_CACHE = "public, max-age=60"


def get_service(db=Depends(get_db)) -> GalleryService:
    return GalleryService(GalleryRepository(db), get_storage())


@router.get("/gallery/seasons", response_model=ApiResponse[list[GallerySeason]])
def public_seasons(response: Response, service: GalleryService = Depends(get_service)):
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return ApiResponse(success=True, message="Gallery seasons fetched.", data=service.public_seasons())


@router.get("/gallery", response_model=ApiResponse[GalleryPage])
def public_gallery(
    response: Response,
    season_id: int = Query(...),
    cursor: str | None = None,
    limit: int = 24,
    service: GalleryService = Depends(get_service),
):
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return ApiResponse(
        success=True,
        message="Gallery photos fetched.",
        data=service.public_page(season_id, cursor, limit),
    )


@router.get("/gallery/{item_id}/file")
def download_gallery_file(item_id: int, service: GalleryService = Depends(get_service)):
    data, media_type, name = service.public_file(item_id)
    return RawResponse(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "private, no-store",
        },
    )


@router.get("/gallery/{item_id}", response_model=ApiResponse[GalleryPhoto])
def public_photo(item_id: int, response: Response, service: GalleryService = Depends(get_service)):
    response.headers["Cache-Control"] = _PUBLIC_CACHE
    return ApiResponse(success=True, message="Gallery photo fetched.", data=service.public_photo(item_id))


@router.get(
    "/admin/gallery",
    response_model=ApiResponse[GalleryPage],
    dependencies=[Depends(require_permission(*_ANY_GALLERY))],
)
def admin_gallery(
    season_id: int | None = None,
    status: str = "all",
    q: str | None = None,
    cursor: str | None = None,
    limit: int = 48,
    service: GalleryService = Depends(get_service),
):
    return ApiResponse(
        success=True,
        message="Gallery photos fetched.",
        data=service.admin_page(season_id, status, q, cursor, limit),
    )


@router.post(
    "/gallery",
    response_model=ApiResponse[GalleryPhoto],
    dependencies=[Depends(require_permission("gallery.create"))],
)
def create_gallery(
    admin: AdminResponse = Depends(require_permission("gallery.create")),
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
    season_ids: str | None = Form(None),
    caption: str | None = Form(None),
    alt_text: str | None = Form(None),
    width: int | None = Form(None),
    height: int | None = Form(None),
    taken_on: str | None = Form(None),
    service: GalleryService = Depends(get_service),
):
    try:
        taken = parse_taken_on(taken_on)
    except ValueError as error:
        raise HTTPException(status_code=400, detail="Taken on must be a date or a date and time.") from error
    photo = service.create(
        admin,
        file=file,
        url=url,
        season_ids=season_ids,
        caption=caption,
        alt_text=alt_text,
        width=width,
        height=height,
        taken_on=taken,
    )
    return ApiResponse(success=True, message="Saved as a draft.", data=photo)


@router.patch("/gallery/{item_id}", response_model=ApiResponse[GalleryPhoto])
def update_gallery(
    item_id: int,
    payload: GalleryUpdateRequest,
    admin: AdminResponse = Depends(require_permission("gallery.update")),
    service: GalleryService = Depends(get_service),
):
    photo = service.update(item_id, payload, admin, payload.model_fields_set)
    return ApiResponse(success=True, message="Photo updated.", data=photo)


@router.put("/gallery/order", response_model=ApiResponse[GalleryPage])
def order_gallery(
    payload: GalleryOrderRequest,
    _: AdminResponse = Depends(require_permission("gallery.update")),
    service: GalleryService = Depends(get_service),
):
    return ApiResponse(success=True, message="Gallery order saved.", data=service.order(payload))


@router.put("/seasons/{season_id}/gallery-cover", response_model=ApiResponse[GalleryPhoto])
def gallery_cover(
    season_id: int,
    payload: GalleryCoverRequest,
    _: AdminResponse = Depends(require_permission("gallery.update")),
    service: GalleryService = Depends(get_service),
):
    return ApiResponse(success=True, message="Season cover saved.", data=service.cover(season_id, payload))


@router.post("/gallery/bulk", response_model=ApiResponse[GalleryBulkResult])
def bulk_gallery(
    payload: GalleryBulkRequest,
    admin: AdminResponse = Depends(require_permission("gallery.update")),
    service: GalleryService = Depends(get_service),
):
    return ApiResponse(success=True, message="Gallery updated.", data=service.bulk(payload, admin))


@router.delete("/gallery/{item_id}", response_model=ApiResponse[bool])
def delete_gallery(
    item_id: int,
    admin: AdminResponse = Depends(require_permission("gallery.delete")),
    service: GalleryService = Depends(get_service),
):
    service.delete(item_id, admin)
    return ApiResponse(success=True, message="Photo removed.", data=True)
