"""Helper test Leave: tanggal relatif hari ini (zona waktu tenant) dan pembuatan data lewat API."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from httpx import AsyncClient, Response

from app.models import Role, Tenant
from tests.conftest import AuthHeaders, MakeTenant
from tests.core_hr_helpers import create_employee, create_org_unit

TENANT_TZ = ZoneInfo("Asia/Jakarta")


def tenant_today() -> date:
    return datetime.now(TENANT_TZ).date()


def monday_after(days: int) -> date:
    """Senin pertama minimal `days` hari dari hari ini, supaya jumlah hari kerja pasti."""
    target = tenant_today() + timedelta(days=days)
    return target + timedelta(days=(7 - target.weekday()) % 7)


async def create_leave_type(
    client: AsyncClient, headers: dict[str, str], **fields: Any
) -> dict[str, Any]:
    body = {"code": f"LT_{uuid4().hex[:6]}", "name": "Cuti Test"} | fields
    response = await client.post("/leave/types", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def create_policy(
    client: AsyncClient, headers: dict[str, str], leave_type_id: str, **fields: Any
) -> dict[str, Any]:
    body = {"leave_type_id": leave_type_id, "annual_days": 12} | fields
    response = await client.post("/leave/policies", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def submit(
    client: AsyncClient,
    headers: dict[str, str],
    leave_type_id: str,
    start: date,
    end: date,
    *,
    key: str | None = None,
    reason: str | None = None,
) -> Response:
    extra = {"Idempotency-Key": key} if key else {}
    return await client.post(
        "/leave/requests",
        json={
            "leave_type_id": leave_type_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "reason": reason,
        },
        headers=headers | extra,
    )


async def validate(
    client: AsyncClient, headers: dict[str, str], leave_type_id: str, start: date, end: date
) -> dict[str, Any]:
    response = await client.post(
        "/leave/requests/validate",
        json={
            "leave_type_id": leave_type_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


async def balance_of(
    client: AsyncClient, headers: dict[str, str], code: str, **params: Any
) -> dict[str, Any]:
    response = await client.get("/leave/balances", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["items"] if item["leave_type_code"] == code)


async def decide(
    client: AsyncClient, headers: dict[str, str], request_id: str, decision: str
) -> Response:
    return await client.post(
        f"/leave/requests/{request_id}/decision", json={"decision": decision}, headers=headers
    )


@dataclass(slots=True)
class LeaveOrg:
    """Direktur (tanpa atasan) → manager → staff dan peer, plus tipe cuti tahunan dan sakit."""

    tenant: Tenant
    hr: dict[str, str]
    unit_id: str
    ids: dict[str, str]
    headers: dict[str, dict[str, str]]
    annual: dict[str, Any]
    sick: dict[str, Any]


async def build_leave_org(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> LeaveOrg:
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    unit = await create_org_unit(client, hr)
    director = await create_employee(client, hr, unit["id"], full_name="Direktur")
    manager = await create_employee(
        client, hr, unit["id"], full_name="Manager", supervisor_id=director["id"]
    )
    staff = await create_employee(
        client, hr, unit["id"], full_name="Staf", supervisor_id=manager["id"]
    )
    peer = await create_employee(
        client, hr, unit["id"], full_name="Rekan", supervisor_id=manager["id"]
    )
    ids = {
        "director": director["id"],
        "manager": manager["id"],
        "staff": staff["id"],
        "peer": peer["id"],
    }
    roles = {"director": Role.MANAGER, "manager": Role.MANAGER}
    headers = {
        name: auth_headers(tenant, roles.get(name, Role.EMPLOYEE), employee_id=UUID(emp_id))
        for name, emp_id in ids.items()
    }
    annual = await create_leave_type(
        client,
        hr,
        code="CUTI_TAHUNAN",
        name="Cuti Tahunan",
        min_notice_days=3,
        max_days_per_request=10,
    )
    await create_policy(client, hr, annual["id"], annual_days=12, min_service_months=12)
    sick = await create_leave_type(
        client, hr, code="SAKIT", name="Sakit", requires_balance=False, allow_backdated=True
    )
    return LeaveOrg(
        tenant=tenant,
        hr=hr,
        unit_id=unit["id"],
        ids=ids,
        headers=headers,
        annual=annual,
        sick=sick,
    )
