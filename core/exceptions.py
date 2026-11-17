import logging

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import ValidationError

from core.response import ApiResponse

logger = logging.getLogger("cpl.errors")

def _plain_errors(errors) -> list[dict]:
    """Validator errors carry the raised exception in ctx, which JSON cannot encode."""
    plain = []
    for item in errors:
        entry = dict(item)
        if "ctx" in entry:
            entry["ctx"] = {key: str(value) for key, value in entry["ctx"].items()}
        if entry.get("type") == "value_error" and isinstance(entry.get("msg"), str):
            entry["msg"] = entry["msg"].removeprefix("Value error, ")
        plain.append(entry)
    return plain


def add_exception_handlers(app):

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=ApiResponse(
                success=False,
                message=str(exc.detail),
                data=None
            ).model_dump()
        )

    @app.exception_handler(StarletteHTTPException)
    async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=ApiResponse(
                success=False,
                message=str(exc.detail),
                data=None
            ).model_dump()
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        errors = _plain_errors(exc.errors())
        custom = [item["msg"] for item in errors if item.get("type") == "value_error"]
        return JSONResponse(
            status_code=422,
            content=ApiResponse(
                success=False,
                message=custom[0] if custom else "Validation failed",
                data=errors
            ).model_dump()
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
        return JSONResponse(
            status_code=500,
            content=ApiResponse(
                success=False,
                message="Response validation failed",
                data=_plain_errors(exc.errors())
            ).model_dump()
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # Database and driver messages name tables, indexes and values, so they stay in the log.
        logger.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=ApiResponse(
                success=False,
                message="Something went wrong on our side. Try again in a moment.",
                data=None
            ).model_dump()
        )
