from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, Role
from tests.conftest import AuthHeaders, MakeTenant
from tests.leave_helpers import create_leave_type, create_policy


@pytest.fixture
async def setup(make_tenant: MakeTenant, auth_headers: AuthHeaders) -> dict[str, Any]:
    tenant = await make_tenant()
    return {
        "tenant": tenant,
        "hr": auth_headers(tenant, Role.HR_ADMIN),
        "employee": auth_headers(tenant, Role.EMPLOYEE),
    }


# --- Tipe cuti -----------------------------------------------------------------------


async def test_leave_type_crud(
    client: AsyncClient,
    setup: dict[str, Any],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hr, employee = setup["hr"], setup["employee"]

    created = await create_leave_type(
        client, hr, code="cuti_tahunan", name="Cuti Tahunan", min_notice_days=3
    )
    assert created["code"] == "CUTI_TAHUNAN"
    assert created["is_active"] is True
    assert created["requires_balance"] is True
    assert created["approval_levels"] == 1

    duplicate = await client.post(
        "/leave/types", json={"code": "Cuti_Tahunan", "name": "Dobel"}, headers=hr
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "leave_type_code_taken"

    updated = await client.patch(
        f"/leave/types/{created['id']}",
        json={"max_days_per_request": 5, "approval_levels": 2},
        headers=hr,
    )
    assert updated.status_code == 200
    assert updated.json()["max_days_per_request"] == 5
    assert updated.json()["approval_levels"] == 2

    cleared = await client.patch(
        f"/leave/types/{created['id']}", json={"max_days_per_request": None}, headers=hr
    )
    assert cleared.json()["max_days_per_request"] is None

    # Karyawan melihat tipe aktif untuk form, tapi tidak melihat yang nonaktif.
    listed = await client.get("/leave/types", headers=employee)
    assert [t["code"] for t in listed.json()["items"]] == ["CUTI_TAHUNAN"]

    await client.patch(f"/leave/types/{created['id']}", json={"is_active": False}, headers=hr)
    assert (await client.get("/leave/types", headers=employee)).json()["items"] == []
    as_employee = await client.get(
        "/leave/types", params={"include_inactive": True}, headers=employee
    )
    assert as_employee.json()["items"] == []
    as_hr = await client.get("/leave/types", params={"include_inactive": True}, headers=hr)
    assert [t["is_active"] for t in as_hr.json()["items"]] == [False]

    async with admin_sessionmaker() as s:
        actions = await s.scalars(
            select(AuditLog.action)
            .where(AuditLog.entity_id == created["id"])
            .order_by(AuditLog.created_at)
        )
        assert list(actions) == [
            "leave_type.create",
            "leave_type.update",
            "leave_type.update",
            "leave_type.update",
        ]


@pytest.mark.parametrize(
    "body",
    [
        {"code": "", "name": "X"},
        {"code": "ada spasi", "name": "X"},
        {"code": "OK", "name": "X", "approval_levels": 3},
        {"code": "OK", "name": "X", "max_days_per_request": 0},
        {"code": "OK", "name": "X", "min_notice_days": -1},
    ],
)
async def test_leave_type_validation(
    client: AsyncClient, setup: dict[str, Any], body: dict[str, Any]
) -> None:
    response = await client.post("/leave/types", json=body, headers=setup["hr"])
    assert response.status_code == 422


# --- Policy --------------------------------------------------------------------------


async def test_policy_crud(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr = setup["hr"]
    leave_type = await create_leave_type(client, hr)

    base = await create_policy(client, hr, leave_type["id"], annual_days=12, min_service_months=12)
    senior = await create_policy(
        client, hr, leave_type["id"], grade="g5", annual_days=15, min_service_months=12
    )
    assert senior["grade"] == "G5"

    duplicate = await client.post(
        "/leave/policies",
        json={"leave_type_id": leave_type["id"], "annual_days": 20, "min_service_months": 12},
        headers=hr,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "leave_policy_exists"

    updated = await client.patch(
        f"/leave/policies/{base['id']}", json={"annual_days": 14}, headers=hr
    )
    assert updated.json()["annual_days"] == 14

    listed = await client.get(
        "/leave/policies", params={"leave_type_id": leave_type["id"]}, headers=hr
    )
    assert {p["id"] for p in listed.json()["items"]} == {base["id"], senior["id"]}

    deleted = await client.delete(f"/leave/policies/{senior['id']}", headers=hr)
    assert deleted.status_code == 204
    again = await client.delete(f"/leave/policies/{senior['id']}", headers=hr)
    assert again.status_code == 404


async def test_policy_requires_existing_leave_type(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    response = await client.post(
        "/leave/policies",
        json={"leave_type_id": "00000000-0000-0000-0000-000000000000", "annual_days": 12},
        headers=setup["hr"],
    )
    assert response.status_code == 404


# --- Hari libur ----------------------------------------------------------------------


async def test_holidays(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, employee = setup["hr"], setup["employee"]
    body = {"holiday_date": "2030-08-17", "name": "Hari Kemerdekaan"}
    created = await client.post("/leave/holidays", json=body, headers=hr)
    assert created.status_code == 201
    assert created.json()["kind"] == "national"

    await client.post(
        "/leave/holidays",
        json={"holiday_date": "2031-01-01", "name": "Tahun Baru", "kind": "national"},
        headers=hr,
    )
    duplicate = await client.post("/leave/holidays", json=body, headers=hr)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "holiday_exists"

    # Semua karyawan bisa melihat hari libur (untuk kalender dan form).
    listed = await client.get("/leave/holidays", params={"year": 2030}, headers=employee)
    assert [h["name"] for h in listed.json()["items"]] == ["Hari Kemerdekaan"]

    deleted = await client.delete(f"/leave/holidays/{created.json()['id']}", headers=hr)
    assert deleted.status_code == 204
    listed = await client.get("/leave/holidays", params={"year": 2030}, headers=hr)
    assert listed.json()["items"] == []


# --- Otorisasi dan isolasi -----------------------------------------------------------


async def test_non_hr_cannot_configure(
    client: AsyncClient, setup: dict[str, Any], auth_headers: AuthHeaders
) -> None:
    hr = setup["hr"]
    leave_type = await create_leave_type(client, hr)
    policy = await create_policy(client, hr, leave_type["id"])
    calls: list[tuple[str, str, dict[str, Any] | None]] = [
        ("POST", "/leave/types", {"code": "X", "name": "X"}),
        ("PATCH", f"/leave/types/{leave_type['id']}", {"name": "Y"}),
        ("GET", "/leave/policies", None),
        ("POST", "/leave/policies", {"leave_type_id": leave_type["id"], "annual_days": 1}),
        ("PATCH", f"/leave/policies/{policy['id']}", {"annual_days": 1}),
        ("DELETE", f"/leave/policies/{policy['id']}", None),
        ("POST", "/leave/holidays", {"holiday_date": "2030-01-01", "name": "X"}),
    ]
    manager = auth_headers(setup["tenant"], Role.MANAGER)
    for headers in (setup["employee"], manager):
        for method, url, body in calls:
            response = await client.request(method, url, json=body, headers=headers)
            assert response.status_code == 403, (method, url, response.text)


async def test_tenant_isolation(
    client: AsyncClient, setup: dict[str, Any], make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    hr_a = setup["hr"]
    leave_type = await create_leave_type(client, hr_a)
    policy = await create_policy(client, hr_a, leave_type["id"])
    holiday = await client.post(
        "/leave/holidays", json={"holiday_date": "2030-05-01", "name": "Hari Buruh"}, headers=hr_a
    )

    hr_b = auth_headers(await make_tenant(), Role.HR_ADMIN)
    assert (await client.get("/leave/types", headers=hr_b)).json()["items"] == []
    assert (await client.get("/leave/policies", headers=hr_b)).json()["items"] == []
    assert (await client.get("/leave/holidays", params={"year": 2030}, headers=hr_b)).json()[
        "items"
    ] == []

    patched = await client.patch(
        f"/leave/types/{leave_type['id']}", json={"name": "Dibajak"}, headers=hr_b
    )
    assert patched.status_code == 404
    cross_policy = await client.post(
        "/leave/policies", json={"leave_type_id": leave_type["id"], "annual_days": 99}, headers=hr_b
    )
    assert cross_policy.status_code == 404
    assert (await client.delete(f"/leave/policies/{policy['id']}", headers=hr_b)).status_code == 404
    deleted = await client.delete(f"/leave/holidays/{holiday.json()['id']}", headers=hr_b)
    assert deleted.status_code == 404
