"""Login, refresh token (rotasi + deteksi pemakaian ulang), dan logout."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.db import get_sessionmaker
from app.core.logging import get_logger
from app.core.security import (
    AccessClaims,
    InvalidTokenError,
    create_access_token,
    hash_password,
    hash_token,
    new_refresh_token,
    refresh_token_tenant,
    verify_password,
)
from app.core.tenant import set_tenant_context
from app.models import AppUser, RefreshToken, Tenant, UserRole
from app.services.audit import record_audit

logger = get_logger(__name__)


class InvalidCredentialsError(Exception):
    """Tenant, email, atau password salah. Sengaja tidak dibedakan."""


class TooManyAttemptsError(Exception):
    """Terlalu banyak login gagal untuk tenant + email ini."""


class InvalidRefreshTokenError(Exception):
    """Refresh token tidak dikenal, kedaluwarsa, dicabut, atau user nonaktif."""


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


def _attempts_key(tenant_slug: str, email: str) -> str:
    return f"login_fail:{tenant_slug}:{email}"


async def _failed_attempts(redis: Redis, key: str) -> int:
    try:
        return int(await redis.get(key) or 0)
    except RedisError:
        # Redis mati tidak boleh membuat semua orang gagal login.
        logger.warning("login_throttle_unavailable")
        return 0


async def _record_failure(redis: Redis, key: str, settings: Settings) -> None:
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, settings.login_lockout_seconds, nx=True)
            await pipe.execute()
    except RedisError:
        logger.warning("login_throttle_unavailable")


async def _roles(session: AsyncSession, user_id: UUID) -> list[str]:
    rows = await session.scalars(select(UserRole.role).where(UserRole.app_user_id == user_id))
    return sorted(rows)


async def _issue_tokens(
    session: AsyncSession, user: AppUser, settings: Settings
) -> tuple[IssuedTokens, RefreshToken]:
    claims = AccessClaims(
        user_id=user.id,
        tenant_id=user.tenant_id,
        roles=frozenset(await _roles(session, user.id)),
        employee_id=user.employee_id,
    )
    access_token, expires_in = create_access_token(claims, settings)
    raw, token_hash = new_refresh_token(user.tenant_id)
    record = RefreshToken(
        tenant_id=user.tenant_id,
        app_user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days),
    )
    session.add(record)
    await session.flush()
    issued = IssuedTokens(access_token, expires_in, raw, record.expires_at)
    return issued, record


async def login(
    session: AsyncSession,
    redis: Redis,
    settings: Settings,
    *,
    tenant_slug: str,
    email: str,
    password: str,
) -> IssuedTokens:
    tenant_slug = tenant_slug.strip().lower()
    email = email.strip().lower()
    key = _attempts_key(tenant_slug, email)
    if await _failed_attempts(redis, key) >= settings.login_max_attempts:
        raise TooManyAttemptsError

    tenant_id = await session.scalar(
        select(Tenant.id).where(Tenant.slug == tenant_slug, Tenant.is_active.is_(True))
    )
    user: AppUser | None = None
    if tenant_id is not None:
        await set_tenant_context(session, tenant_id)
        user = await session.scalar(
            select(AppUser).where(AppUser.email == email, AppUser.is_active.is_(True))
        )

    ok, needs_rehash = verify_password(password, user.password_hash if user else None)
    if not ok or user is None:
        await _record_failure(redis, key, settings)
        logger.info("login_failed", tenant_slug=tenant_slug)
        raise InvalidCredentialsError

    try:
        await redis.delete(key)
    except RedisError:
        logger.warning("login_throttle_unavailable")

    if needs_rehash:
        user.password_hash = hash_password(password)
    user.last_login_at = datetime.now(UTC)
    tokens, _ = await _issue_tokens(session, user, settings)
    await record_audit(
        session,
        tenant_id=user.tenant_id,
        actor_user_id=user.id,
        action="auth.login",
        entity_type="app_user",
        entity_id=user.id,
    )
    return tokens


async def _revoke_all_for_user(tenant_id: UUID, user_id: UUID) -> None:
    """Dipakai saat refresh token lama dipakai ulang (kemungkinan dicuri).

    Memakai transaksi terpisah supaya pencabutan tetap tersimpan walaupun request-nya gagal.
    """
    async with get_sessionmaker()() as session, session.begin():
        await set_tenant_context(session, tenant_id)
        await session.execute(
            update(RefreshToken)
            .where(RefreshToken.app_user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="auth.refresh_token_reuse",
            entity_type="app_user",
            entity_id=user_id,
        )
    logger.warning("refresh_token_reuse_detected", user_id=str(user_id))


async def refresh(session: AsyncSession, settings: Settings, *, raw_token: str) -> IssuedTokens:
    try:
        tenant_id = refresh_token_tenant(raw_token)
    except InvalidTokenError as exc:
        raise InvalidRefreshTokenError from exc

    await set_tenant_context(session, tenant_id)
    record = await session.scalar(
        select(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw_token))
        .with_for_update()
    )
    if record is None:
        raise InvalidRefreshTokenError
    if record.revoked_at is not None:
        await _revoke_all_for_user(record.tenant_id, record.app_user_id)
        raise InvalidRefreshTokenError
    now = datetime.now(UTC)
    if record.expires_at <= now:
        raise InvalidRefreshTokenError

    user = await session.get(AppUser, record.app_user_id)
    if user is None or not user.is_active:
        raise InvalidRefreshTokenError

    tokens, new_record = await _issue_tokens(session, user, settings)
    record.revoked_at = now
    record.replaced_by_id = new_record.id
    return tokens


async def logout(session: AsyncSession, *, raw_token: str) -> None:
    """Cabut refresh token. Idempotent: token tidak dikenal diabaikan."""
    try:
        tenant_id = refresh_token_tenant(raw_token)
    except InvalidTokenError:
        return
    await set_tenant_context(session, tenant_id)
    user_id = await session.scalar(
        update(RefreshToken)
        .where(
            RefreshToken.token_hash == hash_token(raw_token),
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.now(UTC))
        .returning(RefreshToken.app_user_id)
    )
    if user_id is not None:
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=user_id,
            action="auth.logout",
            entity_type="app_user",
            entity_id=user_id,
        )
