"""Domain exceptions and global exception handlers.

Clients receive generic, structured error bodies. Stack traces, SQL, and
internal details never cross the boundary (Security rule 5.6).
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger("app.error")


class AppError(Exception):
    """Base class for expected domain errors mapped to HTTP responses."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "bad_request"
    message: str = "Bad request."

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or self.message)
        if message:
            self.message = message


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found."


class ValidationError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "validation_error"
    message = "Invalid request."


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    message = "Authentication required."


class AuthorizationError(AppError):
    # Prefer 404 over 403 where revealing existence leaks scope (API conventions).
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "You do not have access to this resource."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Conflicting request."


class ReplayError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "replay_detected"
    message = "Replayed or out-of-order request rejected."


class RateLimitError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests — please slow down."


class StorageError(AppError):
    # Object storage (S3) is misconfigured or unreachable — distinct from a bad
    # request, so the client sees a clean 502 instead of a 500 stack trace.
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "storage_error"
    message = "File storage is unavailable right now. Please try again."


class PayloadTooLargeError(AppError):
    # The request body exceeds the endpoint's cap — reject before buffering it all
    # into memory (avoids a memory-exhaustion DoS on uploads).
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    code = "payload_too_large"
    message = "The upload is too large."


def _error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo *which* fields failed, but never the submitted values (may be PII).
        fields = [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "validation_error",
                    "message": "Request validation failed.",
                    "fields": fields,
                }
            },
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body("http_error", str(exc.detail)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Log the real cause server-side; return an opaque message to the client.
        logger.error(
            "unhandled_exception",
            extra={"path": request.url.path, "method": request.method},
            exc_info=exc,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=_error_body("internal_error", "An internal error occurred."),
        )
