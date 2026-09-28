"""Error bisnis standar. Service melempar error ini, handler di main.py mengubahnya jadi response
`{"detail": {"code": ..., "message": ...}}`, jadi router tidak perlu try/except.
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(AppError):
    """Data tidak ada, atau ada tapi user tidak berhak melihatnya (sengaja tidak dibedakan)."""

    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class RuleViolationError(AppError):
    """Hard validation dari rules engine gagal. Transaksi diblokir."""

    status_code = 422
    code = "rule_violation"


async def app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # pragma: no cover - handler hanya didaftarkan untuk AppError
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )
