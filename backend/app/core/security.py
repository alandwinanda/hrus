"""Password hashing (argon2id), access token JWT, dan refresh token opaque."""

import hashlib
import secrets
import time
from dataclasses import dataclass
from functools import lru_cache
from uuid import UUID, uuid4

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings

JWT_ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 32
# Nilai JWT_SECRET di .env.example diawali prefix ini dan ditolak di production.
DEV_SECRET_PREFIX = "dev-only"  # noqa: S105 - penanda nilai dev, bukan secret
ACCESS_TOKEN_TYPE = "access"  # noqa: S105 - nilai claim "type", bukan secret

_hasher = PasswordHasher()


class InvalidTokenError(Exception):
    """Token tidak valid, kedaluwarsa, atau salah tipe."""


# --- Password ---------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash(secrets.token_urlsafe(16))


def verify_password(password: str, password_hash: str | None) -> tuple[bool, bool]:
    """Kembalikan (cocok, perlu_rehash).

    Kalau user tidak ada (password_hash None), tetap verifikasi ke hash palsu supaya waktu
    respons sama dan email terdaftar tidak bisa ditebak dari lamanya proses.
    """
    target = password_hash or _dummy_hash()
    try:
        _hasher.verify(target, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False, False
    if password_hash is None:
        return False, False
    return True, _hasher.check_needs_rehash(password_hash)


# --- Access token (JWT) -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessClaims:
    user_id: UUID
    tenant_id: UUID
    roles: frozenset[str]
    employee_id: UUID | None


def _secret(settings: Settings) -> str:
    secret = settings.jwt_secret.get_secret_value() if settings.jwt_secret else ""
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(f"JWT_SECRET belum diisi atau kurang dari {MIN_SECRET_LENGTH} karakter")
    return secret


def ensure_auth_configured(settings: Settings) -> None:
    """Dipanggil saat startup supaya konfigurasi yang salah ketahuan sejak awal."""
    secret = _secret(settings)
    if settings.app_env == "production":
        if secret.startswith(DEV_SECRET_PREFIX):
            raise RuntimeError("JWT_SECRET masih memakai nilai dev dari .env.example")
        if not settings.cookie_secure:
            raise RuntimeError("COOKIE_SECURE wajib true di production")


def create_access_token(claims: AccessClaims, settings: Settings) -> tuple[str, int]:
    now = int(time.time())
    ttl = settings.access_token_ttl_seconds
    payload = {
        "iss": settings.jwt_issuer,
        "sub": str(claims.user_id),
        "tid": str(claims.tenant_id),
        "roles": sorted(claims.roles),
        "eid": str(claims.employee_id) if claims.employee_id else None,
        "type": ACCESS_TOKEN_TYPE,
        "iat": now,
        "exp": now + ttl,
        "jti": uuid4().hex,
    }
    return jwt.encode(payload, _secret(settings), algorithm=JWT_ALGORITHM), ttl


def decode_access_token(token: str, settings: Settings) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            _secret(settings),
            algorithms=[JWT_ALGORITHM],
            issuer=settings.jwt_issuer,
            options={"require": ["exp", "iat", "sub", "tid", "type"]},
        )
        if payload["type"] != ACCESS_TOKEN_TYPE:
            raise InvalidTokenError("bukan access token")
        return AccessClaims(
            user_id=UUID(payload["sub"]),
            tenant_id=UUID(payload["tid"]),
            roles=frozenset(payload.get("roles") or []),
            employee_id=UUID(payload["eid"]) if payload.get("eid") else None,
        )
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        raise InvalidTokenError(str(exc)) from exc


# --- Refresh token (opaque) ---------------------------------------------------
# Format: "<tenant_id>.<acak>". tenant_id dibutuhkan untuk set tenant context (RLS) sebelum
# mencari token. Yang disimpan di database hanya hash SHA-256 dari seluruh token.


def new_refresh_token(tenant_id: UUID) -> tuple[str, str]:
    raw = f"{tenant_id}.{secrets.token_urlsafe(32)}"
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def refresh_token_tenant(raw: str) -> UUID:
    try:
        tenant_part, secret_part = raw.split(".", 1)
        if not secret_part:
            raise ValueError("kosong")
        return UUID(tenant_part)
    except ValueError as exc:
        raise InvalidTokenError("format refresh token tidak valid") from exc
