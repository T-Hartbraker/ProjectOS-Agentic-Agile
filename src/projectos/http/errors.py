"""Structured JSON errors shared by HTTP handlers."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from projectos.errors import (
    AuthorizationError,
    ConflictError,
    CrossProjectWriteError,
    GitRepositoryError,
    OrchestrationError,
    ProjectOSError,
    ProjectctlError,
    RegistryConflictError,
    RegistryError,
    RepositoryValidationError,
)


def correlation_id_of(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "") or "")


def error_payload(code: str, message: str, correlation_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "correlation_id": correlation_id,
        }
    }


def json_error(status_code: int, code: str, message: str, correlation_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=error_payload(code, message, correlation_id),
        headers={"X-Correlation-ID": correlation_id, "X-Request-ID": correlation_id},
    )


def map_projectos_error(exc: ProjectOSError) -> tuple[int, str]:
    if isinstance(exc, AuthorizationError):
        return 403, "forbidden"
    if isinstance(exc, RegistryConflictError):
        return 409, "conflict"
    if isinstance(exc, RegistryError):
        text = str(exc).lower()
        if "not in the registry" in text or "not found" in text:
            return 404, "not_found"
        return 400, "registry_error"
    if isinstance(exc, ProjectctlError):
        return 422, "projectctl_error"
    if isinstance(exc, GitRepositoryError):
        return 422, "git_error"
    if isinstance(exc, RepositoryValidationError):
        return 422, "validation_error"
    if isinstance(exc, CrossProjectWriteError):
        return 409, "cross_project"
    if isinstance(exc, ConflictError):
        return 409, "conflict"
    if isinstance(exc, OrchestrationError):
        if "not found" in str(exc).lower():
            return 404, "not_found"
        return 409, "orchestration_error"
    return 400, "request_error"


def register_exception_handlers(app) -> None:
    @app.exception_handler(ProjectOSError)
    async def projectos_error_handler(request: Request, exc: ProjectOSError) -> JSONResponse:
        status, code = map_projectos_error(exc)
        cid = correlation_id_of(request)
        return json_error(status, code, str(exc), cid)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        cid = correlation_id_of(request)
        messages = "; ".join(
            f"{'.'.join(str(loc) for loc in err.get('loc', ()))}: {err.get('msg')}"
            for err in exc.errors()
        )
        return json_error(422, "validation_error", messages, cid)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        cid = correlation_id_of(request)
        code = "not_found" if exc.status_code == 404 else "http_error"
        return json_error(int(exc.status_code), code, str(exc.detail), cid)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, StarletteHTTPException):
            return await http_error_handler(request, exc)
        cid = correlation_id_of(request)
        return json_error(500, "internal_error", "internal server error", cid)
