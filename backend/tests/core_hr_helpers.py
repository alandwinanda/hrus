"""Helper test Core HR: membuat data lewat API (jalur yang sama dengan user sungguhan)."""

from datetime import date
from typing import Any
from uuid import uuid4

from httpx import AsyncClient

HIRE_DATE = date(2024, 1, 2)


async def create_org_unit(
    client: AsyncClient, headers: dict[str, str], **fields: Any
) -> dict[str, Any]:
    body = {"code": f"U-{uuid4().hex[:8]}", "name": "Unit Test"} | fields
    response = await client.post("/org-units", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def create_employee(
    client: AsyncClient,
    headers: dict[str, str],
    org_unit_id: str,
    *,
    hire_date: date = HIRE_DATE,
    supervisor_id: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    body = {
        "employee_number": f"E{uuid4().hex[:8].upper()}",
        "full_name": "Karyawan Test",
        "hire_date": hire_date.isoformat(),
        "job": {
            "job_title": "Staff",
            "grade": "G3",
            "org_unit_id": org_unit_id,
            "supervisor_employee_id": supervisor_id,
        },
    } | fields
    response = await client.post("/employees", json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()
