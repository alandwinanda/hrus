"""Dependency bersama: user login, cek role, dan session DB dengan tenant context."""

from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import transaction_session
from app.core.logging import bind_tenant
from app.core.security import AccessClaims, InvalidTokenError, decode_access_token
from app.core.tenant import set_tenant_context
from app.models import Role

SettingsDep = Annotated[Settings, Depends(get_settings)]

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"code": code, "message": message},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: SettingsDep,
) -> AccessClaims:
    """User dari access token. tenant_id selalu dari token, tidak pernah dari header/body."""
    if credentials is None:
        raise _unauthorized("not_authenticated", "Butuh access token.")
    try:
        user = decode_access_token(credentials.credentials, settings)
    except InvalidTokenError as exc:
        raise _unauthorized("invalid_token", "Access token tidak valid atau kedaluwarsa.") from exc
    bind_tenant(user.tenant_id)
    structlog.contextvars.bind_contextvars(user_id=str(user.user_id))
    return user


CurrentUserDep = Annotated[AccessClaims, Depends(get_current_user)]


def require_roles(*roles: Role) -> Callable[..., Awaitable[AccessClaims]]:
    """Dependency: user wajib punya minimal satu dari role yang disebut."""
    allowed = {str(r) for r in roles}

    async def dependency(user: CurrentUserDep) -> AccessClaims:
        if not user.roles & allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"code": "forbidden", "message": "Role Anda tidak punya akses ke sini."},
            )
        return user

    return dependency


# scope="function": commit selesai sebelum response dikirim, jadi client tidak pernah menerima
# sukses untuk data yang gagal tersimpan.
TxSessionDep = Annotated[AsyncSession, Depends(transaction_session, scope="function")]


async def get_tenant_session(user: CurrentUserDep, session: TxSessionDep) -> AsyncSession:
    await set_tenant_context(session, user.tenant_id)
    return session


# Session untuk semua endpoint bisnis: satu transaksi per request, RLS aktif untuk tenant user.
TenantSessionDep = Annotated[AsyncSession, Depends(get_tenant_session)]
