from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, LeaveBalance, Role
from app.rules import leave as rules
from tests.conftest import AuthHeaders, MakeTenant
from tests.core_hr_helpers import create_employee, create_org_unit
from tests.job_helpers import InlineDispatch, run_job
from tests.leave_helpers import create_leave_type, create_policy, tenant_today

# Semua skenario memakai tahun lalu supaya as_of selalu <= hari ini.
YEAR = tenant_today().year - 1
SENIOR_HIRE = date(YEAR - 3, 1, 2)


@pytest.fixture
async def setup(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> dict[str, Any]:
    """Cuti tahunan: 12 hari setelah 12 bulan, 15 hari untuk grade G5, carry-over maksimal 6
    hari yang hangus 3 bulan (1 April)."""
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    unit = await create_org_unit(client, hr)
    annual = await create_leave_type(client, hr, code="CUTI_TAHUNAN")
    await create_policy(
        client,
        hr,
        annual["id"],
        annual_days=12,
        min_service_months=12,
        max_carry_over_days=6,
        carry_over_expiry_months=3,
    )
    await create_policy(client, hr, annual["id"], grade="G5", annual_days=15, min_service_months=12)
    # Tipe tanpa saldo tidak ikut accrual.
    await create_leave_type(client, hr, code="SAKIT", requires_balance=False)
    return {"tenant": tenant, "hr": hr, "unit": unit["id"], "annual": annual}


async def _employee(client: AsyncClient, setup: dict[str, Any], **fields: Any) -> str:
    employee = await create_employee(client, setup["hr"], setup["unit"], **fields)
    return str(employee["id"])


async def _balance(
    client: AsyncClient, setup: dict[str, Any], employee_id: str, year: int = YEAR
) -> dict[str, Any]:
    response = await client.get(
        "/leave/balances", params={"employee_id": employee_id, "year": year}, headers=setup["hr"]
    )
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


async def _accrue(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch, as_of: date
) -> dict[str, Any]:
    run = await run_job(client, setup["hr"], dispatcher, "leave_accrual", as_of=as_of.isoformat())
    assert run["status"] == "success", run
    return run["output"]["counts"]


async def _expire(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch, as_of: date
) -> dict[str, Any]:
    run = await run_job(
        client, setup["hr"], dispatcher, "leave_carry_over_expiry", as_of=as_of.isoformat()
    )
    assert run["status"] == "success", run
    return run["output"]["counts"]


async def test_balances_for_active_employees_only(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    make_tenant: MakeTenant,
    auth_headers: AuthHeaders,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    today = tenant_today()
    active = await _employee(client, setup, hire_date=SENIOR_HIRE)
    leaver = await _employee(client, setup, hire_date=SENIOR_HIRE)
    await client.post(
        f"/employees/{leaver}/jobs",
        json={"effdt": (today - timedelta(days=10)).isoformat(), "action": "termination"},
        headers=setup["hr"],
    )
    await _employee(client, setup, hire_date=today + timedelta(days=10))  # belum mulai

    # Tenant lain tidak tersentuh.
    other = await make_tenant()
    other_hr = auth_headers(other, Role.HR_ADMIN)
    other_unit = await create_org_unit(client, other_hr)
    await create_employee(client, other_hr, other_unit["id"], hire_date=SENIOR_HIRE)
    await create_leave_type(client, other_hr, code="CUTI_TAHUNAN")

    counts = await _accrue(client, setup, dispatcher, today)
    assert counts == {"balance_created": 1}
    balance = await _balance(client, setup, active, today.year)
    assert (balance["entitled"], balance["available"]) == (12, 12)
    async with admin_sessionmaker() as s:
        balances = list(
            await s.scalars(
                select(LeaveBalance.employee_id).where(LeaveBalance.tenant_id == other.id)
            )
        )
    assert balances == []


async def test_anniversary_and_promotion_raise_entitlement(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch
) -> None:
    newcomer = await _employee(client, setup, hire_date=date(YEAR - 1, 3, 1))
    senior = await _employee(client, setup, hire_date=SENIOR_HIRE)

    counts = await _accrue(client, setup, dispatcher, date(YEAR, 1, 2))
    assert counts == {"balance_created": 2}
    assert (await _balance(client, setup, newcomer))["entitled"] == 0  # baru 10 bulan
    assert (await _balance(client, setup, senior))["entitled"] == 12

    await client.post(
        f"/employees/{senior}/jobs",
        json={"effdt": date(YEAR, 5, 1).isoformat(), "action": "promotion", "grade": "G5"},
        headers=setup["hr"],
    )
    counts = await _accrue(client, setup, dispatcher, date(YEAR, 6, 15))
    assert counts == {"entitlement_increased": 2}
    assert (await _balance(client, setup, newcomer))["entitled"] == 12  # 1 Maret: 12 bulan
    assert (await _balance(client, setup, senior))["entitled"] == 15

    # Turun grade tidak mengurangi jatah yang sudah diberikan.
    await client.post(
        f"/employees/{senior}/jobs",
        json={"effdt": date(YEAR, 7, 1).isoformat(), "action": "data_change", "grade": "G3"},
        headers=setup["hr"],
    )
    assert await _accrue(client, setup, dispatcher, date(YEAR, 7, 2)) == {}
    assert (await _balance(client, setup, senior))["entitled"] == 15


async def test_carry_over_and_expiry(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    saver = await _employee(client, setup, hire_date=SENIOR_HIRE)
    spender = await _employee(client, setup, hire_date=SENIOR_HIRE)
    async with admin_sessionmaker() as s, s.begin():
        for employee_id, used in ((saver, 4), (spender, 9)):
            s.add(
                LeaveBalance(
                    tenant_id=setup["tenant"].id,
                    employee_id=employee_id,
                    leave_type_id=setup["annual"]["id"],
                    year=YEAR - 1,
                    entitled=12,
                    used=used,
                )
            )

    counts = await _accrue(client, setup, dispatcher, date(YEAR, 1, 2))
    assert counts == {"balance_created": 2, "carried_over": 2}
    saved = await _balance(client, setup, saver)
    assert (saved["carried_over"], saved["carry_over_expires_on"]) == (6, f"{YEAR}-04-01")
    assert saved["available"] == 18  # sisa 8, dibatasi 6
    assert (await _balance(client, setup, spender))["carried_over"] == 3
    assert await _accrue(client, setup, dispatcher, date(YEAR, 1, 3)) == {}  # tidak dobel

    # Pakai sebagian carry-over sebelum hangus: saver pakai 2 + ajukan 1, spender pakai 5.
    async with admin_sessionmaker() as s, s.begin():
        for employee_id, used, pending in ((saver, 2, 1), (spender, 5, 0)):
            await s.execute(
                update(LeaveBalance)
                .where(LeaveBalance.employee_id == employee_id, LeaveBalance.year == YEAR)
                .values(used=used, pending=pending)
            )

    assert await _expire(client, setup, dispatcher, date(YEAR, 3, 31)) == {}
    counts = await _expire(client, setup, dispatcher, date(YEAR, 4, 1))
    assert counts == {"carry_over_expired": 1, "carry_over_fully_used": 1}
    after = await _balance(client, setup, saver)
    assert (after["expired"], after["available"]) == (3, 12)  # 12 + 6 - 2 - 1 - 3
    assert (await _balance(client, setup, spender))["expired"] == 0
    assert await _expire(client, setup, dispatcher, date(YEAR, 4, 2)) == {}

    async with admin_sessionmaker() as s:
        audit = await s.scalar(
            select(AuditLog.after).where(
                AuditLog.tenant_id == setup["tenant"].id,
                AuditLog.action == "leave_balance.carry_over_expired",
            )
        )
    assert audit is not None
    assert audit["expired"] == 3
    assert "job_run_id" in audit


async def test_no_carry_over_without_last_year_balance(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch
) -> None:
    """Saldo awal dari sistem lama dimasukkan lewat koreksi saldo, bukan ditebak job."""
    employee = await _employee(client, setup, hire_date=SENIOR_HIRE)
    assert await _accrue(client, setup, dispatcher, date(YEAR, 1, 2)) == {"balance_created": 1}
    assert (await _balance(client, setup, employee))["carried_over"] == 0


@pytest.mark.parametrize(
    ("months", "expected"),
    [(0, None), (3, date(2026, 4, 1)), (12, date(2027, 1, 1)), (14, date(2027, 3, 1))],
)
def test_carry_over_expiry_date(months: int, expected: date | None) -> None:
    assert rules.carry_over_expiry(2026, months) == expected


@pytest.mark.parametrize(
    ("hire", "as_of", "months"),
    [
        (date(2025, 3, 1), date(2026, 3, 1), 12),
        (date(2025, 3, 15), date(2026, 3, 14), 11),
        (date(2026, 5, 1), date(2026, 1, 1), 0),
    ],
)
def test_service_months(hire: date, as_of: date, months: int) -> None:
    assert rules.service_months(hire, as_of) == months
