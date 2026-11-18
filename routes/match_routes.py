from fastapi import APIRouter, Depends
from typing import List
from core.database import get_db
from core.response import ApiResponse
from repositories.match_repository import MatchRepository
from services.match_service import MatchService
from controllers.match_controller import MatchController
from routes.auth_routes import require_permission
from services.notify.enqueue import load_notice, publish_match_change, publish_side_effects
from services.notify.flags import load_flags
from schemas.match_schema import (
    MatchCreateRequest,
    MatchUpdateRequest,
    MatchResponse,
    MatchOrderResponse,
    MatchStatsUpsertRequest,
    MatchStatResponse
)


router = APIRouter(
    prefix="/matches",
    tags=["Matches"]
)


def get_controller(db=Depends(get_db)) -> MatchController:
    repository = MatchRepository(db)
    service = MatchService(repository)
    return MatchController(service)


def _armed() -> bool:
    try:
        return load_flags().delivery_ready()
    except Exception:
        return False


def _publish(db, controller: MatchController, kind: str, before, after, match_id: int | None):
    if not _armed():
        return
    publish_match_change(db, kind, before, after)
    seen = {match_id} if match_id else set()
    publish_side_effects(db, controller.service.repository.notices, seen)


@router.post("", response_model=ApiResponse[MatchResponse], dependencies=[Depends(require_permission("matches.create"))])
def create(request: MatchCreateRequest, db=Depends(get_db), controller: MatchController = Depends(get_controller)):
    data = controller.create(request)
    _publish(db, controller, "created", None, load_notice(db, data.id) if _armed() else None, data.id)
    return ApiResponse(
        success=True,
        message="Match created successfully",
        data=data
    )



@router.get("", response_model=ApiResponse[List[MatchResponse]])
def get_all(seasonId: int = None, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Matches fetched successfully",
        data=controller.get_all(season_id=seasonId)
    )



@router.get("/next-order", response_model=ApiResponse[MatchOrderResponse])
def get_next_match_order(seasonId: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Next match order fetched successfully",
        data=controller.get_next_match_order(seasonId)
    )



@router.get("/{match_id}", response_model=ApiResponse[MatchResponse])
def get_by_id(match_id: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match fetched successfully",
        data=controller.get_by_id(match_id)
   )


@router.patch("/{match_id}", response_model=ApiResponse[MatchResponse], dependencies=[Depends(require_permission("matches.update"))])
def update(match_id: int, request: MatchUpdateRequest, db=Depends(get_db), controller: MatchController = Depends(get_controller)):
    before = load_notice(db, match_id) if _armed() else None
    data = controller.update(match_id, request)
    _publish(db, controller, "updated", before, load_notice(db, match_id) if _armed() else None, match_id)
    return ApiResponse(
        success=True,
        message="Match updated successfully",
        data=data
    )


@router.post("/{match_id}/reset", response_model=ApiResponse[MatchResponse], dependencies=[Depends(require_permission("matches.reset"))])
def reset(match_id: int, db=Depends(get_db), controller: MatchController = Depends(get_controller)):
    before = load_notice(db, match_id) if _armed() else None
    data = controller.reset(match_id)
    _publish(db, controller, "reset", before, load_notice(db, match_id) if _armed() else None, match_id)
    return ApiResponse(
        success=True,
        message="Match reset to scheduled",
        data=data
    )


@router.get("/{match_id}/stats", response_model=ApiResponse[List[MatchStatResponse]])
def get_stats(match_id: int, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match stats fetched successfully",
        data=controller.get_stats(match_id)
    )


@router.put("/{match_id}/stats", response_model=ApiResponse[List[MatchStatResponse]], dependencies=[Depends(require_permission("matches.scorecard"))])
def upsert_stats(match_id: int, request: MatchStatsUpsertRequest, controller: MatchController = Depends(get_controller)):
    return ApiResponse(
        success=True,
        message="Match stats updated successfully",
        data=controller.upsert_stats(match_id, request)
    )


@router.delete("/{match_id}", response_model=ApiResponse[bool], dependencies=[Depends(require_permission("matches.delete"))])
def delete(match_id: int, db=Depends(get_db), controller: MatchController = Depends(get_controller)):
    before = load_notice(db, match_id) if _armed() else None
    data = controller.delete(match_id)
    after = None
    if _armed():
        after = load_notice(db, match_id) or ({**before, "void": True} if before else None)
    _publish(db, controller, "deleted", before, after, match_id)
    return ApiResponse(
        success=True,
        message="Match deleted successfully",
        data=data
    )
