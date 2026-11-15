import time
from collections import defaultdict, deque
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from core.response import ApiResponse
from routes.auth_routes import get_current_admin
from schemas.auth_schema import AdminResponse
from services.assistant import actions
from services.assistant.engine import NAME, ChatResult, Engine
from services.assistant.executor import ToolExecutor
from services.assistant.providers import configured_providers
from services.assistant.readiness import usable_labels

router = APIRouter(prefix="/assistant", tags=["Assistant"])

MAX_MESSAGE = 1000
MAX_HISTORY = 12
MAX_HISTORY_TEXT = 2000


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., min_length=1)

    @field_validator("content")
    @classmethod
    def _trim(cls, value: str) -> str:
        return value[:MAX_HISTORY_TEXT]


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE)
    history: list[ChatTurn] = Field(default_factory=list)

    @field_validator("message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Message cannot be empty.")
        return value

    @field_validator("history")
    @classmethod
    def _recent(cls, value: list[ChatTurn]) -> list[ChatTurn]:
        return value[-MAX_HISTORY:]


class AdminChatRequest(ChatRequest):
    pending: str | None = Field(None, max_length=8000)


class ConfirmRequest(BaseModel):
    token: str = Field(..., min_length=10, max_length=8000)
    approve: bool = True


class ChatSource(BaseModel):
    tool: str
    label: str


class ChatDetail(BaseModel):
    label: str
    value: str


class ChatAction(BaseModel):
    token: str
    tool: str
    title: str
    details: list[ChatDetail]
    destructive: bool
    expires_in: int


class ChatResponse(BaseModel):
    reply: str
    sources: list[ChatSource] = Field(default_factory=list)
    action: ChatAction | None = None
    changed: bool = False
    provider: str | None = None
    available: bool = True


class AssistantStatus(BaseModel):
    name: str
    available: bool
    providers: list[str]


class RateLimiter:
    def __init__(self, limit: int, window: float = 60.0):
        self.limit = limit
        self.window = window
        self.hits: dict[str, deque] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        hits = self.hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"{NAME} needs a breather. Please wait a moment and try again.",
            )
        hits.append(now)
        if len(self.hits) > 5000:
            for stale in [name for name, items in self.hits.items() if not items or now - items[-1] > self.window]:
                self.hits.pop(stale, None)


public_limiter = RateLimiter(limit=20)
admin_limiter = RateLimiter(limit=40)
OFF_REPLY = "Striker is turned off. An admin can switch it on from Settings."


def _off_response():
    return ApiResponse(success=True, message=None, data=ChatResponse(reply=OFF_REPLY))


async def assistant_is_enabled(app) -> bool:
    """Read the switch and release the connection before any model call. Unreadable means off."""
    executor = ToolExecutor(app, public=True)
    try:
        code, payload = await executor._call("GET", "/settings")
    except Exception:
        return False
    finally:
        await executor.close()
    data = payload.get("data") if code == 200 and isinstance(payload, dict) else None
    return bool(data.get("assistant_enabled")) if isinstance(data, dict) else False


def _client_key(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _run(request: Request, mode: str, admin: AdminResponse | None, work):
    async with httpx.AsyncClient() as llm_client:
        executor = ToolExecutor(
            request.app,
            authorization=request.headers.get("authorization") if admin else None,
            public=mode == "public",
        )
        try:
            engine = Engine(mode, executor, configured_providers(llm_client), admin=admin)
            result = await work(engine)
        finally:
            await executor.close()
    return ApiResponse(success=True, message=None, data=ChatResponse(**result.as_dict()))


@router.get("/status", response_model=ApiResponse[AssistantStatus])
async def assistant_status():
    async with httpx.AsyncClient() as client:
        providers = await usable_labels(client)
    return ApiResponse(
        success=True,
        message=None,
        data=AssistantStatus(name=NAME, available=bool(providers), providers=providers),
    )


@router.post("/public/chat", response_model=ApiResponse[ChatResponse])
async def public_chat(payload: ChatRequest, request: Request):
    public_limiter.check(_client_key(request))
    if not await assistant_is_enabled(request.app):
        return _off_response()
    history = [turn.model_dump() for turn in payload.history]
    return await _run(request, "public", None, lambda engine: engine.chat(payload.message, history))


@router.post("/admin/chat", response_model=ApiResponse[ChatResponse])
async def admin_chat(
    payload: AdminChatRequest,
    request: Request,
    admin: AdminResponse = Depends(get_current_admin),
):
    admin_limiter.check(f"admin:{admin.id}")
    if not await assistant_is_enabled(request.app):
        return _off_response()
    history = [turn.model_dump() for turn in payload.history]

    async def work(engine: Engine):
        if payload.pending and actions.is_yes(payload.message):
            return await engine.confirm(payload.pending)
        if payload.pending and actions.is_no(payload.message):
            return ChatResult(reply="Okay, I've cancelled that. Nothing was changed.")
        return await engine.chat(payload.message, history)

    return await _run(request, "admin", admin, work)


@router.post("/admin/confirm", response_model=ApiResponse[ChatResponse])
async def admin_confirm(
    payload: ConfirmRequest,
    request: Request,
    admin: AdminResponse = Depends(get_current_admin),
):
    admin_limiter.check(f"admin:{admin.id}")
    if not await assistant_is_enabled(request.app):
        return _off_response()

    async def work(engine: Engine):
        if not payload.approve:
            return ChatResult(reply="Okay, I've cancelled that. Nothing was changed.")
        return await engine.confirm(payload.token)

    return await _run(request, "admin", admin, work)
