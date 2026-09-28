from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, Role, Tenant
from tests.conftest import AuthHeaders, MakeTenant
from tests.core_hr_helpers import HIRE_DATE, create_employee, create_org_unit

TODAY = date.today()


@pytest.fixture
async def setup(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> dict[str, Any]:
    """Tenant dengan dua unit, satu atasan, dan header HR."""
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    unit = await create_org_unit(client, hr, code="ENG")
    other_unit = await create_org_unit(client, hr, code="OPS")
    manager = await create_employee(client, hr, unit["id"], full_name="Bu Atasan")
    return {
        "tenant": tenant,
        "hr": hr,
        "unit": unit,
        "other_unit": other_unit,
        "manager": manager,
    }


async def _add_job(client: AsyncClient, hr: dict[str, str], employee_id: str, **body: Any) -> Any:
    return await client.post(f"/employees/{employee_id}/jobs", json=body, headers=hr)


# --- Create dan baca ---------------------------------------------------------------


async def test_create_employee_with_hire_job(
    client: AsyncClient,
    setup: dict[str, Any],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hr, unit, manager = setup["hr"], setup["unit"], setup["manager"]

    employee = await create_employee(
        client,
        hr,
        unit["id"],
        supervisor_id=manager["id"],
        employee_number="emp-001",
        work_email="Budi@Acme.co.id",
    )

    assert employee["employee_number"] == "EMP-001"
    assert employee["work_email"] == "budi@acme.co.id"
    assert employee["status"] == "active"
    assert employee["current_job"] == {
        "effdt": HIRE_DATE.isoformat(),
        "effseq": 0,
        "action": "hire",
        "job_title": "Staff",
        "grade": "G3",
        "org_unit_id": unit["id"],
        "supervisor_employee_id": manager["id"],
        "employment_type": "permanent",
        "employment_status": "active",
    }
    async with admin_sessionmaker() as s:
        audit = await s.scalar(
            select(AuditLog.after).where(
                AuditLog.entity_id == employee["id"], AuditLog.action == "employee.create"
            )
        )
    assert audit is not None
    assert audit["job"]["action"] == "hire"


async def test_future_hire_is_pre_hire(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, unit = setup["hr"], setup["unit"]
    future = TODAY + timedelta(days=30)

    employee = await create_employee(client, hr, unit["id"], hire_date=future)
    as_of_start = await client.get(
        f"/employees/{employee['id']}", params={"as_of": future.isoformat()}, headers=hr
    )
    pre_hire = (await client.get("/employees", params={"status": "pre_hire"}, headers=hr)).json()

    assert employee["status"] == "pre_hire"
    assert employee["current_job"] is None
    assert as_of_start.json()["status"] == "active"
    assert [e["id"] for e in pre_hire["items"]] == [employee["id"]]


@pytest.mark.parametrize(
    ("field", "code"),
    [("employee_number", "employee_number_taken"), ("work_email", "work_email_taken")],
)
async def test_duplicates_conflict(
    client: AsyncClient, setup: dict[str, Any], field: str, code: str
) -> None:
    hr, unit = setup["hr"], setup["unit"]
    value = "DUP-1" if field == "employee_number" else "dup@acme.co.id"
    await create_employee(client, hr, unit["id"], **{field: value})

    response = await client.post(
        "/employees",
        json={
            "employee_number": "OTHER-1" if field == "work_email" else value.lower(),
            "full_name": "X",
            "hire_date": HIRE_DATE.isoformat(),
            "work_email": value if field == "work_email" else None,
            "job": {"job_title": "S", "grade": "G1", "org_unit_id": unit["id"]},
        },
        headers=hr,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == code


async def test_create_rejects_unknown_references(
    client: AsyncClient, setup: dict[str, Any], make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    hr, unit = setup["hr"], setup["unit"]
    other_tenant = await make_tenant()
    other_hr = auth_headers(other_tenant, Role.HR_ADMIN)
    foreign_unit = await create_org_unit(client, other_hr)
    foreign_employee = await create_employee(client, other_hr, foreign_unit["id"])
    base = {"employee_number": "REF-1", "full_name": "X", "hire_date": HIRE_DATE.isoformat()}

    wrong_unit = await client.post(
        "/employees",
        json=base | {"job": {"job_title": "S", "grade": "G1", "org_unit_id": foreign_unit["id"]}},
        headers=hr,
    )
    wrong_supervisor = await client.post(
        "/employees",
        json=base
        | {
            "job": {
                "job_title": "S",
                "grade": "G1",
                "org_unit_id": unit["id"],
                "supervisor_employee_id": foreign_employee["id"],
            }
        },
        headers=hr,
    )

    assert wrong_unit.status_code == 422
    assert wrong_unit.json()["detail"]["code"] == "org_unit_invalid"
    assert wrong_supervisor.status_code == 422
    assert wrong_supervisor.json()["detail"]["code"] == "employee_invalid"


@pytest.mark.parametrize(
    "patch",
    [
        {"employee_number": ""},
        {"employee_number": "ada spasi"},
        {"work_email": "bukan-email"},
        {"hire_date": "2024-13-01"},
        {"job": {"job_title": "S", "grade": "G1"}},
        {
            "job": {
                "job_title": "S",
                "grade": "G1",
                "org_unit_id": "x",
                "employment_type": "freelance",
            }
        },
    ],
)
async def test_create_validation(
    client: AsyncClient, setup: dict[str, Any], patch: dict[str, Any]
) -> None:
    body = {
        "employee_number": "VAL-1",
        "full_name": "X",
        "hire_date": HIRE_DATE.isoformat(),
        "job": {"job_title": "S", "grade": "G1", "org_unit_id": setup["unit"]["id"]},
    } | patch

    response = await client.post("/employees", json=body, headers=setup["hr"])

    assert response.status_code == 422


async def test_update_basic_data(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, unit = setup["hr"], setup["unit"]
    employee = await create_employee(client, hr, unit["id"], work_email="lama@acme.co.id")

    renamed = await client.patch(
        f"/employees/{employee['id']}", json={"full_name": "Nama Baru"}, headers=hr
    )
    cleared = await client.patch(
        f"/employees/{employee['id']}", json={"work_email": None}, headers=hr
    )

    assert renamed.json()["full_name"] == "Nama Baru"
    assert renamed.json()["work_email"] == "lama@acme.co.id"
    assert cleared.json()["work_email"] is None


# --- List ---------------------------------------------------------------------------


async def test_list_paginates_and_filters(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, unit, other, manager = setup["hr"], setup["unit"], setup["other_unit"], setup["manager"]
    in_eng = [
        await create_employee(client, hr, unit["id"], supervisor_id=manager["id"]) for _ in range(3)
    ]
    in_ops = await create_employee(client, hr, other["id"])

    everyone: list[str] = []
    cursor: str | None = None
    while True:
        params = {"limit": 2} | ({"cursor": cursor} if cursor else {})
        body = (await client.get("/employees", params=params, headers=hr)).json()
        everyone += [e["id"] for e in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    by_unit = await client.get("/employees", params={"org_unit_id": other["id"]}, headers=hr)
    by_supervisor = await client.get(
        "/employees", params={"supervisor_id": manager["id"]}, headers=hr
    )

    assert sorted(everyone) == sorted([manager["id"], in_ops["id"], *(e["id"] for e in in_eng)])
    assert len(everyone) == len(set(everyone))
    assert [e["id"] for e in by_unit.json()["items"]] == [in_ops["id"]]
    assert sorted(e["id"] for e in by_supervisor.json()["items"]) == sorted(e["id"] for e in in_eng)


async def test_list_status_filter(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, unit = setup["hr"], setup["unit"]
    leaver = await create_employee(client, hr, unit["id"])
    await _add_job(client, hr, leaver["id"], effdt=TODAY.isoformat(), action="termination")

    active = (await client.get("/employees", headers=hr)).json()["items"]
    terminated = (
        await client.get("/employees", params={"status": "terminated"}, headers=hr)
    ).json()
    everyone = (await client.get("/employees", params={"status": "all"}, headers=hr)).json()

    assert leaver["id"] not in [e["id"] for e in active]
    assert [e["id"] for e in terminated["items"]] == [leaver["id"]]
    assert leaver["id"] in [e["id"] for e in everyone["items"]]


# --- Riwayat jabatan (effective-dated) -----------------------------------------------


async def test_transfer_carries_forward_fields(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, unit, other, manager = setup["hr"], setup["unit"], setup["other_unit"], setup["manager"]
    employee = await create_employee(client, hr, unit["id"], supervisor_id=manager["id"])

    response = await _add_job(
        client, hr, employee["id"], effdt="2024-06-01", action="transfer", org_unit_id=other["id"]
    )

    job = response.json()
    assert response.status_code == 201
    assert job["org_unit_id"] == other["id"]
    assert job["job_title"] == "Staff"
    assert job["grade"] == "G3"
    assert job["supervisor_employee_id"] == manager["id"]
    assert job["effseq"] == 0
    assert job["employment_status"] == "active"


async def test_same_day_change_gets_next_effseq(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, unit = setup["hr"], setup["unit"]
    employee = await create_employee(client, hr, unit["id"])

    first = await _add_job(
        client, hr, employee["id"], effdt="2024-06-01", action="promotion", grade="G4"
    )
    second = await _add_job(
        client,
        hr,
        employee["id"],
        effdt="2024-06-01",
        action="data_change",
        job_title="Senior Staff",
    )
    current = (await client.get(f"/employees/{employee['id']}", headers=hr)).json()["current_job"]

    assert first.json()["effseq"] == 0
    assert second.json()["effseq"] == 1
    assert (current["grade"], current["job_title"], current["effseq"]) == ("G4", "Senior Staff", 1)


async def test_future_dated_change_respects_as_of(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    hr, unit, other = setup["hr"], setup["unit"], setup["other_unit"]
    employee = await create_employee(client, hr, unit["id"])
    future = TODAY + timedelta(days=10)
    await _add_job(
        client,
        hr,
        employee["id"],
        effdt=future.isoformat(),
        action="transfer",
        org_unit_id=other["id"],
    )

    today_view = (await client.get(f"/employees/{employee['id']}", headers=hr)).json()
    future_view = (
        await client.get(
            f"/employees/{employee['id']}", params={"as_of": future.isoformat()}, headers=hr
        )
    ).json()

    assert today_view["current_job"]["org_unit_id"] == unit["id"]
    assert future_view["current_job"]["org_unit_id"] == other["id"]


async def test_termination_and_rehire(client: AsyncClient, setup: dict[str, Any]) -> None:
    hr, unit = setup["hr"], setup["unit"]
    employee = await create_employee(client, hr, unit["id"])

    terminated = await _add_job(
        client, hr, employee["id"], effdt="2024-08-01", action="termination", reason="Resign"
    )
    after_termination = await _add_job(
        client, hr, employee["id"], effdt="2024-09-01", action="transfer"
    )
    rehired = await _add_job(client, hr, employee["id"], effdt="2024-10-01", action="rehire")
    rehire_again = await _add_job(client, hr, employee["id"], effdt="2024-11-01", action="rehire")

    assert terminated.json()["employment_status"] == "terminated"
    assert after_termination.status_code == 422
    assert after_termination.json()["detail"]["code"] == "employee_terminated"
    assert rehired.json()["employment_status"] == "active"
    assert rehire_again.json()["detail"]["code"] == "rehire_requires_termination"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"effdt": "2023-12-31", "action": "transfer"}, "effdt_before_hire"),
        ({"effdt": "2024-02-01", "action": "hire"}, "hire_only_on_create"),
        (
            {
                "effdt": "2024-03-01",
                "action": "transfer",
                "org_unit_id": "00000000-0000-0000-0000-000000000000",
            },
            "org_unit_invalid",
        ),
    ],
)
async def test_job_rules(
    client: AsyncClient, setup: dict[str, Any], body: dict[str, str], code: str
) -> None:
    hr, unit = setup["hr"], setup["unit"]
    employee = await create_employee(client, hr, unit["id"])

    response = await _add_job(client, hr, employee["id"], **body)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code


async def test_job_out_of_sequence_and_own_supervisor(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    hr, unit = setup["hr"], setup["unit"]
    employee = await create_employee(client, hr, unit["id"])
    await _add_job(client, hr, employee["id"], effdt="2024-06-01", action="promotion")

    backdated = await _add_job(client, hr, employee["id"], effdt="2024-05-01", action="transfer")
    own = await _add_job(
        client,
        hr,
        employee["id"],
        effdt="2024-07-01",
        action="data_change",
        supervisor_employee_id=employee["id"],
    )

    assert backdated.json()["detail"]["code"] == "effdt_out_of_sequence"
    assert own.json()["detail"]["code"] == "own_supervisor"


async def test_job_history_newest_first_with_pagination(
    client: AsyncClient, setup: dict[str, Any]
) -> None:
    hr, unit = setup["hr"], setup["unit"]
    employee = await create_employee(client, hr, unit["id"])
    for month in ("03", "06", "09"):
        await _add_job(client, hr, employee["id"], effdt=f"2024-{month}-01", action="data_change")

    first = (
        await client.get(f"/employees/{employee['id']}/jobs", params={"limit": 3}, headers=hr)
    ).json()
    second = (
        await client.get(
            f"/employees/{employee['id']}/jobs",
            params={"limit": 3, "cursor": first["next_cursor"]},
            headers=hr,
        )
    ).json()

    dates = [j["effdt"] for j in first["items"] + second["items"]]
    assert dates == ["2024-09-01", "2024-06-01", "2024-03-01", HIRE_DATE.isoformat()]
    assert second["next_cursor"] is None


# --- Otorisasi dan visibilitas ---------------------------------------------------


async def test_visibility_rules(
    client: AsyncClient, setup: dict[str, Any], auth_headers: AuthHeaders
) -> None:
    tenant: Tenant = setup["tenant"]
    hr, unit, manager = setup["hr"], setup["unit"], setup["manager"]
    report = await create_employee(client, hr, unit["id"], supervisor_id=manager["id"])
    stranger = await create_employee(client, hr, unit["id"])

    as_manager = auth_headers(tenant, Role.MANAGER, Role.EMPLOYEE, employee_id=manager["id"])
    as_report = auth_headers(tenant, Role.EMPLOYEE, employee_id=report["id"])

    def get(employee: dict[str, Any], headers: dict[str, str], suffix: str = "") -> Any:
        return client.get(f"/employees/{employee['id']}{suffix}", headers=headers)

    assert (await get(report, as_manager)).status_code == 200
    assert (await get(stranger, as_manager)).status_code == 404
    assert (await get(report, as_report)).status_code == 200
    assert (await get(manager, as_report)).status_code == 404
    assert (await get(report, as_report, "/jobs")).status_code == 200
    assert (await get(report, as_manager, "/jobs")).status_code == 404


@pytest.mark.parametrize("role", [Role.EMPLOYEE, Role.MANAGER])
async def test_non_hr_cannot_list_or_write(
    client: AsyncClient, setup: dict[str, Any], auth_headers: AuthHeaders, role: Role
) -> None:
    headers = auth_headers(setup["tenant"], role)
    target = setup["manager"]["id"]

    assert (await client.get("/employees", headers=headers)).status_code == 403
    assert (await client.post("/employees", json={}, headers=headers)).status_code == 403
    assert (await client.patch(f"/employees/{target}", json={}, headers=headers)).status_code == 403
    assert (
        await client.post(f"/employees/{target}/jobs", json={}, headers=headers)
    ).status_code == 403


async def test_tenant_isolation(
    client: AsyncClient, setup: dict[str, Any], make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    employee_a = setup["manager"]
    hr_b = auth_headers(await make_tenant(), Role.HR_ADMIN)

    listed = (await client.get("/employees", params={"status": "all"}, headers=hr_b)).json()
    fetched = await client.get(f"/employees/{employee_a['id']}", headers=hr_b)
    patched = await client.patch(
        f"/employees/{employee_a['id']}", json={"full_name": "Dibajak"}, headers=hr_b
    )
    job = await _add_job(client, hr_b, employee_a["id"], effdt="2024-06-01", action="transfer")
    jobs = await client.get(f"/employees/{employee_a['id']}/jobs", headers=hr_b)

    assert listed["items"] == []
    assert fetched.status_code == 404
    assert patched.status_code == 404
    assert job.status_code == 404
    assert jobs.status_code == 404
