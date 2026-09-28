import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, Role
from tests.conftest import AuthHeaders, MakeTenant
from tests.core_hr_helpers import create_org_unit


async def test_create_and_get_org_unit(
    client: AsyncClient,
    make_tenant: MakeTenant,
    auth_headers: AuthHeaders,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)

    created = await create_org_unit(client, hr, code="hr-ops", name="  HR Operations ")
    fetched = await client.get(f"/org-units/{created['id']}", headers=hr)

    assert created["code"] == "HR-OPS"
    assert created["name"] == "HR Operations"
    assert created["is_active"] is True
    assert fetched.json() == created
    async with admin_sessionmaker() as s:
        action = await s.scalar(
            select(AuditLog.action).where(
                AuditLog.tenant_id == tenant.id, AuditLog.entity_id == created["id"]
            )
        )
    assert action == "org_unit.create"


async def test_list_paginates_by_code(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    codes = ["E", "A", "D", "B", "C"]
    for code in codes:
        await create_org_unit(client, hr, code=code)

    seen: list[str] = []
    cursor: str | None = None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = (await client.get("/org-units", params=params, headers=hr)).json()
        seen += [item["code"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == sorted(codes)


async def test_inactive_units_hidden_by_default(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    unit = await create_org_unit(client, hr)
    await client.patch(f"/org-units/{unit['id']}", json={"is_active": False}, headers=hr)

    default = (await client.get("/org-units", headers=hr)).json()["items"]
    everything = (
        await client.get("/org-units", params={"include_inactive": True}, headers=hr)
    ).json()["items"]

    assert default == []
    assert [u["id"] for u in everything] == [unit["id"]]


async def test_duplicate_code_conflict(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    await create_org_unit(client, hr, code="FIN")

    response = await client.post("/org-units", json={"code": "fin", "name": "X"}, headers=hr)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "org_unit_code_taken"


async def test_same_code_allowed_in_other_tenant(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant_a, tenant_b = await make_tenant(), await make_tenant()
    await create_org_unit(client, auth_headers(tenant_a, Role.HR_ADMIN), code="FIN")

    await create_org_unit(client, auth_headers(tenant_b, Role.HR_ADMIN), code="FIN")


async def test_parent_hierarchy_and_cycle_rules(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    root = await create_org_unit(client, hr, code="ROOT")
    child = await create_org_unit(client, hr, code="CHILD", parent_id=root["id"])
    grandchild = await create_org_unit(client, hr, code="GRAND", parent_id=child["id"])

    to_grandchild = await client.patch(
        f"/org-units/{root['id']}", json={"parent_id": grandchild["id"]}, headers=hr
    )
    to_self = await client.patch(
        f"/org-units/{root['id']}", json={"parent_id": root["id"]}, headers=hr
    )
    detach = await client.patch(
        f"/org-units/{grandchild['id']}", json={"parent_id": None}, headers=hr
    )

    assert to_grandchild.status_code == 422
    assert to_grandchild.json()["detail"]["code"] == "org_unit_cycle"
    assert to_self.status_code == 422
    assert detach.status_code == 200
    assert detach.json()["parent_id"] is None


async def test_cannot_deactivate_unit_with_active_children(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    parent = await create_org_unit(client, hr)
    await create_org_unit(client, hr, parent_id=parent["id"])

    response = await client.patch(
        f"/org-units/{parent['id']}", json={"is_active": False}, headers=hr
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "org_unit_has_active_children"


async def test_inactive_parent_rejected(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    parent = await create_org_unit(client, hr)
    await client.patch(f"/org-units/{parent['id']}", json={"is_active": False}, headers=hr)

    response = await client.post(
        "/org-units", json={"code": "X1", "name": "X", "parent_id": parent["id"]}, headers=hr
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "org_unit_invalid"


@pytest.mark.parametrize(
    "body",
    [
        {"code": "", "name": "X"},
        {"code": "ada spasi", "name": "X"},
        {"code": "X", "name": ""},
        {"code": "X", "name": "X", "parent_id": "bukan-uuid"},
    ],
)
async def test_create_validation(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders, body: dict[str, str]
) -> None:
    tenant = await make_tenant()

    response = await client.post(
        "/org-units", json=body, headers=auth_headers(tenant, Role.HR_ADMIN)
    )

    assert response.status_code == 422


@pytest.mark.parametrize("role", [Role.EMPLOYEE, Role.MANAGER])
async def test_non_hr_can_read_but_not_write(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders, role: Role
) -> None:
    tenant = await make_tenant()
    unit = await create_org_unit(client, auth_headers(tenant, Role.HR_ADMIN))
    headers = auth_headers(tenant, role)

    assert (await client.get("/org-units", headers=headers)).status_code == 200
    assert (await client.get(f"/org-units/{unit['id']}", headers=headers)).status_code == 200
    create = await client.post("/org-units", json={"code": "Y", "name": "Y"}, headers=headers)
    patch = await client.patch(f"/org-units/{unit['id']}", json={"name": "Z"}, headers=headers)
    assert create.status_code == 403
    assert patch.status_code == 403


async def test_requires_login(client: AsyncClient) -> None:
    assert (await client.get("/org-units")).status_code == 401


async def test_tenant_isolation(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    tenant_a, tenant_b = await make_tenant(), await make_tenant()
    unit_a = await create_org_unit(client, auth_headers(tenant_a, Role.HR_ADMIN))
    hr_b = auth_headers(tenant_b, Role.HR_ADMIN)

    listed = (await client.get("/org-units", headers=hr_b)).json()["items"]
    fetched = await client.get(f"/org-units/{unit_a['id']}", headers=hr_b)
    patched = await client.patch(
        f"/org-units/{unit_a['id']}", json={"name": "Dibajak"}, headers=hr_b
    )
    as_parent = await client.post(
        "/org-units", json={"code": "B1", "name": "B", "parent_id": unit_a["id"]}, headers=hr_b
    )

    assert listed == []
    assert fetched.status_code == 404
    assert patched.status_code == 404
    assert as_parent.status_code == 422
