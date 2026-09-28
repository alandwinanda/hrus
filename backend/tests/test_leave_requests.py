import asyncio
from datetime import date, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, LeaveRequest, Role
from tests.conftest import AuthHeaders, MakeTenant
from tests.core_hr_helpers import create_employee
from tests.leave_helpers import (
    LeaveOrg,
    balance_of,
    build_leave_org,
    create_policy,
    monday_after,
    submit,
    tenant_today,
    validate,
)


@pytest.fixture
async def org(client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders) -> LeaveOrg:
    return await build_leave_org(client, make_tenant, auth_headers)


def _codes(result: dict[str, Any]) -> list[str]:
    return [e["code"] for e in result["errors"]]


# --- Saldo ---------------------------------------------------------------------------


async def test_balance_initialized_from_policy(client: AsyncClient, org: LeaveOrg) -> None:
    staff = org.headers["staff"]
    balance = await balance_of(client, staff, "CUTI_TAHUNAN")
    assert balance | {"leave_type_id": None} == {
        "leave_type_id": None,
        "leave_type_code": "CUTI_TAHUNAN",
        "leave_type_name": "Cuti Tahunan",
        "year": tenant_today().year,
        "entitled": 12,
        "carried_over": 0,
        "adjusted": 0,
        "used": 0,
        "pending": 0,
        "available": 12,
    }
    # Tipe cuti tanpa saldo (sakit) tidak muncul di daftar saldo.
    response = await client.get("/leave/balances", headers=staff)
    assert [i["leave_type_code"] for i in response.json()["items"]] == ["CUTI_TAHUNAN"]


async def test_entitlement_prefers_grade_then_service(client: AsyncClient, org: LeaveOrg) -> None:
    hr = org.hr
    await create_policy(
        client, hr, org.annual["id"], grade="g3", annual_days=14, min_service_months=12
    )
    senior = await create_employee(client, hr, org.unit_id)  # grade G3, masuk 2024
    new_hire = await create_employee(
        client, hr, org.unit_id, hire_date=tenant_today().replace(month=1, day=1)
    )

    senior_balance = await balance_of(client, hr, "CUTI_TAHUNAN", employee_id=senior["id"])
    new_balance = await balance_of(client, hr, "CUTI_TAHUNAN", employee_id=new_hire["id"])
    assert senior_balance["entitled"] == 14
    assert new_balance["entitled"] == 0  # belum 12 bulan per awal tahun


async def test_balance_visibility(
    client: AsyncClient, org: LeaveOrg, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    staff_id = org.ids["staff"]
    for viewer in ("staff", "manager"):
        response = await client.get(
            "/leave/balances", params={"employee_id": staff_id}, headers=org.headers[viewer]
        )
        assert response.status_code == 200, viewer
    for viewer in ("peer", "director"):  # direktur bukan atasan langsung staff
        response = await client.get(
            "/leave/balances", params={"employee_id": staff_id}, headers=org.headers[viewer]
        )
        assert response.status_code == 404, viewer

    other_hr = auth_headers(await make_tenant(), Role.HR_ADMIN)
    response = await client.get(
        "/leave/balances", params={"employee_id": staff_id}, headers=other_hr
    )
    assert response.status_code == 404

    no_profile = await client.get("/leave/balances", headers=org.hr)
    assert no_profile.status_code == 422
    assert no_profile.json()["detail"]["code"] == "no_employee_profile"


async def test_balance_adjustment(
    client: AsyncClient,
    org: LeaveOrg,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    year = tenant_today().year
    body = {
        "employee_id": org.ids["staff"],
        "leave_type_id": org.annual["id"],
        "year": year,
        "delta": 3,
        "note": "Saldo awal dari sistem lama",
    }
    response = await client.post("/leave/balances/adjustments", json=body, headers=org.hr)
    assert response.status_code == 200, response.text
    assert response.json()["adjusted"] == 3
    assert response.json()["available"] == 15

    too_much = await client.post(
        "/leave/balances/adjustments", json=body | {"delta": -16}, headers=org.hr
    )
    assert too_much.status_code == 422
    assert too_much.json()["detail"]["code"] == "balance_negative"

    no_balance = await client.post(
        "/leave/balances/adjustments",
        json=body | {"leave_type_id": org.sick["id"]},
        headers=org.hr,
    )
    assert no_balance.json()["detail"]["code"] == "leave_type_without_balance"

    forbidden = await client.post(
        "/leave/balances/adjustments", json=body, headers=org.headers["manager"]
    )
    assert forbidden.status_code == 403

    async with admin_sessionmaker() as s:
        audit = await s.scalar(
            select(AuditLog.after).where(
                AuditLog.tenant_id == org.tenant.id, AuditLog.action == "leave_balance.adjust"
            )
        )
    assert audit is not None
    assert audit["delta"] == 3


# --- Validasi (dry-run) --------------------------------------------------------------


async def test_validate_is_dry_run(
    client: AsyncClient,
    org: LeaveOrg,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    start = monday_after(14)
    result = await validate(
        client, org.headers["staff"], org.annual["id"], start, start + timedelta(days=2)
    )
    assert result == {
        "valid": True,
        "days": 3,
        "errors": [],
        "warnings": [],
        "balance": {"available_before": 12, "available_after": 9},
    }
    balance = await balance_of(client, org.headers["staff"], "CUTI_TAHUNAN", year=start.year)
    assert balance["pending"] == 0
    async with admin_sessionmaker() as s:
        count = await s.scalar(
            select(LeaveRequest.id).where(LeaveRequest.tenant_id == org.tenant.id).limit(1)
        )
    assert count is None


async def test_holidays_and_weekends_not_counted(client: AsyncClient, org: LeaveOrg) -> None:
    start = monday_after(14)
    tuesday = start + timedelta(days=1)
    await client.post(
        "/leave/holidays",
        json={
            "holiday_date": tuesday.isoformat(),
            "name": "Cuti Bersama",
            "kind": "collective_leave",
        },
        headers=org.hr,
    )
    # Senin sampai Senin berikutnya: 6 hari kerja, dikurangi 1 hari libur.
    result = await validate(
        client, org.headers["staff"], org.annual["id"], start, start + timedelta(days=7)
    )
    assert result["days"] == 5


async def test_validate_reports_every_rule(client: AsyncClient, org: LeaveOrg) -> None:
    staff, annual = org.headers["staff"], org.annual["id"]
    start = monday_after(14)
    next_year = tenant_today().year + 1
    today = tenant_today()
    saturday = start + timedelta(days=5)

    cases: list[tuple[date, date, str]] = [
        (start, start - timedelta(days=1), "invalid_date_range"),
        (date(next_year, 12, 30), date(next_year + 1, 1, 2), "cross_year"),
        (date(next_year, 1, 5), date(next_year, 8, 1), "range_too_long"),
        (saturday, saturday + timedelta(days=1), "no_working_days"),
        (today - timedelta(days=7), today - timedelta(days=5), "start_in_past"),
        (today + timedelta(days=1), today + timedelta(days=2), "insufficient_notice"),
        (start, start + timedelta(days=14), "exceeds_max_days"),
    ]
    for case_start, case_end, code in cases:
        result = await validate(client, staff, annual, case_start, case_end)
        assert result["valid"] is False, code
        assert code in _codes(result), (code, result)

    # Beberapa error sekaligus: saldo tidak cukup dan melebihi batas per pengajuan.
    await client.post(
        "/leave/balances/adjustments",
        json={
            "employee_id": org.ids["staff"],
            "leave_type_id": annual,
            "year": start.year,
            "delta": -10,
            "note": "Koreksi test",
        },
        headers=org.hr,
    )
    result = await validate(client, staff, annual, start, start + timedelta(days=14))
    assert _codes(result) == ["exceeds_max_days", "insufficient_balance"]
    assert result["balance"] == {"available_before": 2, "available_after": -9}


async def test_sick_leave_can_be_backdated(client: AsyncClient, org: LeaveOrg) -> None:
    last_monday = monday_after(0) - timedelta(days=7)
    result = await validate(
        client, org.headers["staff"], org.sick["id"], last_monday, last_monday + timedelta(days=1)
    )
    assert result["valid"] is True
    assert result["balance"] is None


async def test_inactive_type_and_terminated_employee(client: AsyncClient, org: LeaveOrg) -> None:
    start = monday_after(21)
    await client.patch(f"/leave/types/{org.sick['id']}", json={"is_active": False}, headers=org.hr)
    result = await validate(client, org.headers["staff"], org.sick["id"], start, start)
    assert _codes(result) == ["leave_type_inactive"]

    terminated = await client.post(
        f"/employees/{org.ids['peer']}/jobs",
        json={"effdt": (start - timedelta(days=7)).isoformat(), "action": "termination"},
        headers=org.hr,
    )
    assert terminated.status_code == 201, terminated.text
    result = await validate(client, org.headers["peer"], org.annual["id"], start, start)
    assert "employee_not_active" in _codes(result)


async def test_requires_employee_profile(client: AsyncClient, org: LeaveOrg) -> None:
    start = monday_after(14)
    body = {
        "leave_type_id": org.annual["id"],
        "start_date": start.isoformat(),
        "end_date": start.isoformat(),
    }
    for url in ("/leave/requests/validate", "/leave/requests"):
        response = await client.post(url, json=body, headers=org.hr)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "no_employee_profile"
    mine = await client.get("/leave/requests", headers=org.hr)
    assert mine.json()["detail"]["code"] == "no_employee_profile"


async def test_team_overlap_is_warning_only(client: AsyncClient, org: LeaveOrg) -> None:
    start = monday_after(14)
    end = start + timedelta(days=2)
    peer = await submit(client, org.headers["peer"], org.annual["id"], start, end)
    assert peer.status_code == 201

    result = await validate(client, org.headers["staff"], org.annual["id"], start, end)
    assert result["valid"] is True
    assert result["warnings"] == [
        {
            "code": "team_overlap",
            "message": "1 rekan satu tim juga cuti atau mengajukan cuti di tanggal ini.",
            "colleagues_on_leave": 1,
        }
    ]
    submitted = await submit(client, org.headers["staff"], org.annual["id"], start, end)
    assert submitted.json()["warnings"] == result["warnings"]


# --- Submit --------------------------------------------------------------------------


async def test_submit_holds_pending_balance(
    client: AsyncClient,
    org: LeaveOrg,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    staff = org.headers["staff"]
    start = monday_after(14)
    response = await submit(
        client, staff, org.annual["id"], start, start + timedelta(days=2), reason=" Liburan "
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["employee_id"] == org.ids["staff"]
    assert body["employee_name"] == "Staf"
    assert body["leave_type_code"] == "CUTI_TAHUNAN"
    assert body["days"] == 3
    assert body["reason"] == "Liburan"
    assert body["status"] == "pending"
    assert (body["approval_levels"], body["current_level"]) == (1, 1)
    assert [(a["level"], a["approver_employee_id"], a["status"]) for a in body["approvals"]] == [
        (1, org.ids["manager"], "pending")
    ]

    balance = await balance_of(client, staff, "CUTI_TAHUNAN", year=start.year)
    assert (balance["pending"], balance["used"], balance["available"]) == (3, 0, 9)

    async with admin_sessionmaker() as s:
        audit = await s.scalar(
            select(AuditLog.after).where(
                AuditLog.entity_id == body["id"], AuditLog.action == "leave_request.submit"
            )
        )
    assert audit is not None
    assert audit["approvers"] == [org.ids["manager"]]


async def test_submit_rejects_rule_violation(client: AsyncClient, org: LeaveOrg) -> None:
    start = monday_after(14)
    response = await submit(
        client, org.headers["staff"], org.annual["id"], start, start + timedelta(days=14)
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "exceeds_max_days"

    duplicate_first = await submit(client, org.headers["staff"], org.annual["id"], start, start)
    assert duplicate_first.status_code == 201
    overlap = await submit(
        client, org.headers["staff"], org.sick["id"], start, start + timedelta(days=1)
    )
    assert overlap.status_code == 422
    assert overlap.json()["detail"]["code"] == "overlapping_request"


async def test_idempotency_key(client: AsyncClient, org: LeaveOrg) -> None:
    staff = org.headers["staff"]
    start = monday_after(14)
    end = start + timedelta(days=1)
    first = await submit(client, staff, org.annual["id"], start, end, key="chat-msg-0001")
    second = await submit(client, staff, org.annual["id"], start, end, key="chat-msg-0001")
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    balance = await balance_of(client, staff, "CUTI_TAHUNAN", year=start.year)
    assert balance["pending"] == 2  # hanya dihitung sekali

    # Key milik karyawan lain tidak bentrok.
    peer = await submit(
        client, org.headers["peer"], org.annual["id"], start, end, key="chat-msg-0001"
    )
    assert peer.status_code == 201
    assert peer.json()["id"] != first.json()["id"]

    too_short = await submit(client, staff, org.annual["id"], start, end, key="abc")
    assert too_short.status_code == 422


async def test_concurrent_overlapping_submits(client: AsyncClient, org: LeaveOrg) -> None:
    """Dua pengajuan bersamaan yang tumpang tindih: hanya satu yang tersimpan."""
    staff = org.headers["staff"]
    start = monday_after(14)
    for leave_type_id, offset in ((org.sick["id"], 0), (org.annual["id"], 7)):
        day = start + timedelta(days=offset)
        responses = await asyncio.gather(
            submit(client, staff, leave_type_id, day, day + timedelta(days=1)),
            submit(client, staff, leave_type_id, day + timedelta(days=1), day + timedelta(days=2)),
        )
        statuses = sorted(r.status_code for r in responses)
        assert statuses[0] == 201, [r.text for r in responses]
        assert statuses[1] in (409, 422)
        rejected = next(r for r in responses if r.status_code != 201)
        assert rejected.json()["detail"]["code"] == "overlapping_request"

    balance = await balance_of(client, staff, "CUTI_TAHUNAN", year=start.year)
    assert balance["pending"] == 2


async def test_database_blocks_overlap(
    client: AsyncClient,
    org: LeaveOrg,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Exclusion constraint jadi jaring terakhir, bahkan untuk koneksi owner."""
    start = monday_after(14)
    created = await submit(client, org.headers["staff"], org.sick["id"], start, start)
    assert created.status_code == 201

    async def insert_copy(status: str) -> None:
        async with admin_sessionmaker() as s, s.begin():
            original = await s.get(LeaveRequest, created.json()["id"])
            assert original is not None
            s.add(
                LeaveRequest(
                    tenant_id=original.tenant_id,
                    employee_id=original.employee_id,
                    leave_type_id=original.leave_type_id,
                    start_date=original.start_date,
                    end_date=original.end_date,
                    days=1,
                    status=status,
                    approval_levels=1,
                    current_level=1,
                    requested_by_user_id=original.requested_by_user_id,
                    validation={},
                )
            )

    with pytest.raises(IntegrityError, match="ex_leave_request_no_overlap"):
        await insert_copy("pending")
    await insert_copy("rejected")  # yang sudah ditolak tidak ikut dicek
