from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from redis.asyncio import Redis

from app.api.deps import SettingsDep, TxSessionDep
from app.core.config import Settings
from app.core.redis import get_redis
from app.schemas.auth import LoginRequest, TokenResponse
from app.services import auth as auth_service

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_refresh_cookie(
    response: Response, tokens: auth_service.IssuedTokens, settings: Settings
) -> None:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=tokens.refresh_token,
        expires=tokens.refresh_expires_at,
        path=settings.refresh_cookie_path,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )


def _token_response(tokens: auth_service.IssuedTokens) -> TokenResponse:
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@router.post(
    "/login",
    responses={
        status.HTTP_401_UNAUTHORIZED: {"description": "Tenant, email, atau password salah"},
        status.HTTP_429_TOO_MANY_REQUESTS: {"description": "Terlalu banyak percobaan gagal"},
    },
)
async def login(
    body: LoginRequest,
    response: Response,
    session: TxSessionDep,
    settings: SettingsDep,
    redis: Annotated[Redis, Depends(get_redis)],
) -> TokenResponse:
    try:
        tokens = await auth_service.login(
            session,
            redis,
            settings,
            tenant_slug=body.tenant_slug,
            email=body.email,
            password=body.password,
        )
    except auth_service.TooManyAttemptsError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "too_many_attempts",
                "message": "Terlalu banyak percobaan login gagal. Coba lagi nanti.",
            },
        ) from exc
    except auth_service.InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "code": "invalid_credentials",
                "message": "Tenant, email, atau password salah.",
            },
        ) from exc

    _set_refresh_cookie(response, tokens, settings)
    return _token_response(tokens)


@router.post(
    "/refresh",
    responses={status.HTTP_401_UNAUTHORIZED: {"description": "Refresh token tidak valid"}},
)
async def refresh(
    request: Request, response: Response, session: TxSessionDep, settings: SettingsDep
) -> TokenResponse:
    raw_token = request.cookies.get(settings.refresh_cookie_name)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Silakan login ulang."},
        )
    try:
        tokens = await auth_service.refresh(session, settings, raw_token=raw_token)
    except auth_service.InvalidRefreshTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "invalid_refresh_token", "message": "Silakan login ulang."},
        ) from exc

    _set_refresh_cookie(response, tokens, settings)
    return _token_response(tokens)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, session: TxSessionDep, settings: SettingsDep
) -> None:
    """Cabut refresh token. Access token yang sudah terbit tetap berlaku sampai kedaluwarsa."""
    raw_token = request.cookies.get(settings.refresh_cookie_name)
    if raw_token:
        await auth_service.logout(session, raw_token=raw_token)
    _clear_refresh_cookie(response, settings)
