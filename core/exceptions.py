from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from pydantic import ValidationError

from core.response import ApiResponse


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
        return JSONResponse(
            status_code=422,
            content=ApiResponse(
                success=False,
                message="Validation failed",
                data=exc.errors()
            ).model_dump()
        )

    @app.exception_handler(ValidationError)
    async def pydantic_validation_exception_handler(request: Request, exc: ValidationError):
        return JSONResponse(
            status_code=500,
            content=ApiResponse(
                success=False,
                message="Response validation failed",
                data=exc.errors()
            ).model_dump()
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content=ApiResponse(
                success=False,
                message="Internal server error",
                data=str(exc)
            ).model_dump()
        )
