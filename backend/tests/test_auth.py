from datetime import UTC, datetime
from uuid import UUID

import jwt
import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.deps import require_roles
from app.core.config import Settings, get_settings
from app.core.security import AccessClaims, create_access_token
from app.models import AppUser, AuditLog, RefreshToken, Role, Tenant
from tests.conftest import TEST_PASSWORD, MakeTenant, MakeUser

COOKIE = "hrus_refresh"


async def _login(
    client: AsyncClient, tenant: Tenant, user: AppUser, password: str = TEST_PASSWORD
) -> dict[str, object]:
    response = await client.post(
        "/auth/login",
        json={"tenant_slug": tenant.slug, "email": user.email, "password": password},
    )
    return {"status": response.status_code, "body": response.json(), "response": response}


# --- Login -------------------------------------------------------------------


async def test_login_returns_access_token_and_refresh_cookie(
    client: AsyncClient, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant, [Role.HR_ADMIN, Role.EMPLOYEE])

    response = await client.post(
        "/auth/login",
        json={
            "tenant_slug": tenant.slug.upper(),
            "email": user.email.upper(),
            "password": TEST_PASSWORD,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert claims["sub"] == str(user.id)
    assert claims["tid"] == str(tenant.id)
    assert claims["roles"] == ["employee", "hr_admin"]
    assert claims["type"] == "access"

    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}={tenant.id}.")
    assert "HttpOnly" in cookie
    assert "Path=/auth" in cookie
    assert "SameSite=strict" in cookie


@pytest.mark.parametrize("case", ["wrong_password", "unknown_email", "unknown_tenant", "inactive"])
async def test_login_failures_look_identical(
    client: AsyncClient, make_tenant: MakeTenant, make_user: MakeUser, case: str
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant, is_active=case != "inactive")
    payload = {"tenant_slug": tenant.slug, "email": user.email, "password": TEST_PASSWORD}
    if case == "wrong_password":
        payload["password"] = "salah-banget"
    elif case == "unknown_email":
        payload["email"] = "tidak-ada@test.local"
    elif case == "unknown_tenant":
        payload["tenant_slug"] = "tenant-tidak-ada"

    response = await client.post("/auth/login", json=payload)

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_credentials"
    assert "set-cookie" not in response.headers


async def test_login_locked_after_too_many_failures(
    client: AsyncClient, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant)
    for _ in range(get_settings().login_max_attempts):
        result = await _login(client, tenant, user, password="salah-banget")
        assert result["status"] == 401

    result = await _login(client, tenant, user)

    assert result["status"] == 429
    assert result["body"]["detail"]["code"] == "too_many_attempts"  # type: ignore[index]


async def test_login_writes_audit_log(
    client: AsyncClient,
    make_tenant: MakeTenant,
    make_user: MakeUser,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant)

    await _login(client, tenant, user)

    async with admin_sessionmaker() as s:
        actions = list(
            await s.scalars(select(AuditLog.action).where(AuditLog.actor_user_id == user.id))
        )
        last_login = await s.scalar(select(AppUser.last_login_at).where(AppUser.id == user.id))
    assert actions == ["auth.login"]
    assert last_login is not None


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"tenant_slug": "a", "email": "a@b.c"},
        {"tenant_slug": "", "email": "a@b.c", "password": "x"},
    ],
)
async def test_login_validation(client: AsyncClient, body: dict[str, str]) -> None:
    response = await client.post("/auth/login", json=body)

    assert response.status_code == 422


# --- /me dan access token ----------------------------------------------------------


async def test_me_returns_profile(
    client: AsyncClient, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant, [Role.MANAGER, Role.EMPLOYEE])
    token = (await _login(client, tenant, user))["body"]["access_token"]  # type: ignore[index]

    response = await client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json() == {
        "id": str(user.id),
        "email": user.email,
        "roles": ["employee", "manager"],
        "employee_id": None,
        "tenant": {"id": str(tenant.id), "slug": tenant.slug, "name": tenant.name},
    }


def _token(settings: Settings, *, secret: str | None = None, **claims: object) -> str:
    now = int(datetime.now(UTC).timestamp())
    payload = {
        "iss": settings.jwt_issuer,
        "sub": str(UUID(int=1)),
        "tid": str(UUID(int=2)),
        "roles": ["employee"],
        "type": "access",
        "iat": now,
        "exp": now + 60,
    } | claims
    key = secret or settings.jwt_secret.get_secret_value()  # type: ignore[union-attr]
    return jwt.encode(payload, key, algorithm="HS256")


def _use_cookie(client: AsyncClient, value: str) -> None:
    client.cookies.clear()
    client.cookies.set(COOKIE, value)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "Bearer bukan-jwt",
        "Basic dXNlcjpwYXNz",
        "expired",
        "wrong_secret",
        "refresh_type",
        "wrong_issuer",
    ],
)
async def test_me_rejects_bad_tokens(client: AsyncClient, header: str | None) -> None:
    settings = get_settings()
    special = {
        "expired": _token(settings, exp=int(datetime.now(UTC).timestamp()) - 10),
        "wrong_secret": _token(settings, secret="x" * 40),
        "refresh_type": _token(settings, type="refresh"),
        "wrong_issuer": _token(settings, iss="orang-lain"),
    }
    if header in special:
        header = f"Bearer {special[header]}"
    headers = {"Authorization": header} if header else {}

    response = await client.get("/me", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_raw_refresh_token_is_not_an_access_token(
    client: AsyncClient, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant)
    login = (await _login(client, tenant, user))["response"]
    raw_refresh = login.cookies[COOKIE]  # type: ignore[attr-defined]

    response = await client.get("/me", headers={"Authorization": f"Bearer {raw_refresh}"})

    assert response.status_code == 401


# --- Refresh dan logout ------------------------------------------------------------


async def test_refresh_rotates_token(
    client: AsyncClient, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant)
    await _login(client, tenant, user)
    first_cookie = client.cookies[COOKIE]

    response = await client.post("/auth/refresh")

    assert response.status_code == 200
    assert response.json()["access_token"]
    assert client.cookies[COOKIE] != first_cookie


async def test_reusing_old_refresh_token_revokes_all_sessions(
    client: AsyncClient,
    make_tenant: MakeTenant,
    make_user: MakeUser,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant)
    await _login(client, tenant, user)
    stolen = client.cookies[COOKIE]
    assert (await client.post("/auth/refresh")).status_code == 200
    latest = client.cookies[COOKIE]

    _use_cookie(client, stolen)
    reuse = await client.post("/auth/refresh")
    _use_cookie(client, latest)
    after = await client.post("/auth/refresh")

    assert reuse.status_code == 401
    assert after.status_code == 401
    async with admin_sessionmaker() as s:
        active = await s.scalar(
            select(RefreshToken.id).where(
                RefreshToken.app_user_id == user.id, RefreshToken.revoked_at.is_(None)
            )
        )
        actions = set(
            await s.scalars(select(AuditLog.action).where(AuditLog.actor_user_id == user.id))
        )
    assert active is None
    assert "auth.refresh_token_reuse" in actions


async def test_refresh_without_or_with_garbage_cookie(client: AsyncClient) -> None:
    assert (await client.post("/auth/refresh")).status_code == 401
    _use_cookie(client, "bukan-token")
    assert (await client.post("/auth/refresh")).status_code == 401


async def test_refresh_rejected_after_user_deactivated(
    client: AsyncClient,
    make_tenant: MakeTenant,
    make_user: MakeUser,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant)
    await _login(client, tenant, user)
    async with admin_sessionmaker() as s, s.begin():
        (await s.get(AppUser, user.id)).is_active = False  # type: ignore[union-attr]

    assert (await client.post("/auth/refresh")).status_code == 401


async def test_logout_revokes_refresh_token(
    client: AsyncClient, make_tenant: MakeTenant, make_user: MakeUser
) -> None:
    tenant = await make_tenant()
    user = await make_user(tenant)
    await _login(client, tenant, user)
    token = client.cookies[COOKIE]

    response = await client.post("/auth/logout")

    assert response.status_code == 204
    assert COOKIE not in client.cookies
    _use_cookie(client, token)
    assert (await client.post("/auth/refresh")).status_code == 401
    assert (await client.post("/auth/logout")).status_code == 204  # idempotent


# --- require_roles -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "expected"),
    [({Role.EMPLOYEE}, 403), ({Role.HR_ADMIN}, 200), ({Role.MANAGER, Role.EMPLOYEE}, 403)],
)
async def test_require_roles(roles: set[Role], expected: int) -> None:
    app = FastAPI()

    @app.get("/hr-only", dependencies=[Depends(require_roles(Role.HR_ADMIN))])
    async def hr_only() -> dict[str, bool]:
        return {"ok": True}

    claims = AccessClaims(
        user_id=UUID(int=1), tenant_id=UUID(int=2), roles=frozenset(roles), employee_id=None
    )
    token, _ = create_access_token(claims, get_settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        response = await c.get("/hr-only", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == expected
