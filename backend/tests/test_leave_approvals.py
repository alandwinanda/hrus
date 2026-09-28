from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AuditLog, Role
from tests.conftest import AuthHeaders, MakeTenant
from tests.leave_helpers import (
    LeaveOrg,
    balance_of,
    build_leave_org,
    decide,
    monday_after,
    submit,
)


@pytest.fixture
async def org(client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders) -> LeaveOrg:
    return await build_leave_org(client, make_tenant, auth_headers)


async def _submit_ok(
    client: AsyncClient,
    org: LeaveOrg,
    who: str,
    *,
    weeks: int = 2,
    days: int = 3,
    sick: bool = False,
) -> dict[str, Any]:
    start = monday_after(7 * weeks)
    leave_type = org.sick if sick else org.annual
    response = await submit(
        client, org.headers[who], leave_type["id"], start, start + timedelta(days=days - 1)
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _approvals(client: AsyncClient, headers: dict[str, str]) -> list[str]:
    response = await client.get("/leave/requests", params={"scope": "approvals"}, headers=headers)
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


async def _use_two_levels(client: AsyncClient, org: LeaveOrg) -> None:
    response = await client.patch(
        f"/leave/types/{org.annual['id']}", json={"approval_levels": 2}, headers=org.hr
    )
    assert response.status_code == 200


# --- Approval ------------------------------------------------------------------------


async def test_manager_approves(
    client: AsyncClient,
    org: LeaveOrg,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    request = await _submit_ok(client, org, "staff")
    manager = org.headers["manager"]
    assert await _approvals(client, manager) == [request["id"]]

    response = await client.post(
        f"/leave/requests/{request['id']}/decision",
        json={"decision": "approved", "note": " Oke "},
        headers=manager,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert [(a["status"], a["note"]) for a in body["approvals"]] == [("approved", "Oke")]
    assert body["approvals"][0]["decided_at"] is not None

    year = monday_after(14).year
    balance = await balance_of(client, org.headers["staff"], "CUTI_TAHUNAN", year=year)
    assert (balance["pending"], balance["used"], balance["available"]) == (0, 3, 9)
    assert await _approvals(client, manager) == []

    again = await decide(client, manager, request["id"], "rejected")
    assert again.status_code == 422
    assert again.json()["detail"]["code"] == "request_not_pending"

    async with admin_sessionmaker() as s:
        audit = await s.scalar(
            select(AuditLog.after).where(
                AuditLog.entity_id == request["id"], AuditLog.action == "leave_request.approved"
            )
        )
    assert audit == {"level": 1, "status": "approved", "note": "Oke", "as_hr_override": False}


async def test_reject_releases_pending(client: AsyncClient, org: LeaveOrg) -> None:
    request = await _submit_ok(client, org, "staff")
    response = await decide(client, org.headers["manager"], request["id"], "rejected")
    assert response.json()["status"] == "rejected"

    balance = await balance_of(
        client, org.headers["staff"], "CUTI_TAHUNAN", year=monday_after(14).year
    )
    assert (balance["pending"], balance["used"], balance["available"]) == (0, 0, 12)

    # Tanggal yang sama boleh diajukan lagi setelah ditolak.
    await _submit_ok(client, org, "staff")


async def test_two_level_approval(client: AsyncClient, org: LeaveOrg) -> None:
    await _use_two_levels(client, org)
    request = await _submit_ok(client, org, "staff")
    assert [(a["level"], a["approver_employee_id"]) for a in request["approvals"]] == [
        (1, org.ids["manager"]),
        (2, org.ids["director"]),
    ]
    director = org.headers["director"]

    # Direktur approver level 2: boleh melihat, tapi belum gilirannya.
    early = await decide(client, director, request["id"], "approved")
    assert early.status_code == 403
    assert early.json()["detail"]["code"] == "not_current_approver"
    assert await _approvals(client, director) == []

    level_one = await decide(client, org.headers["manager"], request["id"], "approved")
    assert (level_one.json()["status"], level_one.json()["current_level"]) == ("pending", 2)
    assert await _approvals(client, org.headers["manager"]) == []
    assert await _approvals(client, director) == [request["id"]]

    final = await decide(client, director, request["id"], "approved")
    assert final.json()["status"] == "approved"
    assert [a["status"] for a in final.json()["approvals"]] == ["approved", "approved"]


async def test_reject_at_first_level_skips_the_rest(client: AsyncClient, org: LeaveOrg) -> None:
    await _use_two_levels(client, org)
    request = await _submit_ok(client, org, "staff")
    response = await decide(client, org.headers["manager"], request["id"], "rejected")
    assert response.json()["status"] == "rejected"
    assert [a["status"] for a in response.json()["approvals"]] == ["rejected", "skipped"]
    assert await _approvals(client, org.headers["director"]) == []


async def test_level_without_supervisor_goes_to_hr(
    client: AsyncClient,
    org: LeaveOrg,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    request = await _submit_ok(client, org, "director", sick=True)
    assert request["approvals"][0]["approver_employee_id"] is None
    assert request["id"] in await _approvals(client, org.hr)
    assert await _approvals(client, org.headers["manager"]) == []

    not_visible = await decide(client, org.headers["manager"], request["id"], "approved")
    assert not_visible.status_code == 404

    approved = await decide(client, org.hr, request["id"], "approved")
    assert approved.json()["status"] == "approved"
    async with admin_sessionmaker() as s:
        audit = await s.scalar(
            select(AuditLog.after).where(
                AuditLog.entity_id == request["id"], AuditLog.action == "leave_request.approved"
            )
        )
    assert audit is not None
    assert audit["as_hr_override"] is True


async def test_hr_cannot_decide_own_request(
    client: AsyncClient, org: LeaveOrg, auth_headers: AuthHeaders
) -> None:
    hr_staff = auth_headers(org.tenant, Role.HR_ADMIN, employee_id=UUID(org.ids["staff"]))
    request = await _submit_ok(client, org, "staff")
    response = await decide(client, hr_staff, request["id"], "approved")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "cannot_decide_own_request"


async def test_decision_authorization(client: AsyncClient, org: LeaveOrg) -> None:
    request = await _submit_ok(client, org, "staff")
    # Pemilik bisa melihat pengajuannya (403), peer dan direktur tidak (404).
    expected = {"staff": 403, "peer": 404, "director": 404}
    for who, status_code in expected.items():
        response = await decide(client, org.headers[who], request["id"], "approved")
        assert response.status_code == status_code, who

    invalid = await client.post(
        f"/leave/requests/{request['id']}/decision", json={"decision": "maybe"}, headers=org.hr
    )
    assert invalid.status_code == 422


# --- Cancel --------------------------------------------------------------------------


async def test_cancel_pending_and_approved(client: AsyncClient, org: LeaveOrg) -> None:
    staff = org.headers["staff"]
    year = monday_after(14).year

    pending = await _submit_ok(client, org, "staff", weeks=2)
    cancelled = await client.post(
        f"/leave/requests/{pending['id']}/cancel", json={"note": "Batal"}, headers=staff
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert [a["status"] for a in cancelled.json()["approvals"]] == ["skipped"]

    approved = await _submit_ok(client, org, "staff", weeks=3)
    await decide(client, org.headers["manager"], approved["id"], "approved")
    assert (await balance_of(client, staff, "CUTI_TAHUNAN", year=year))["used"] == 3
    response = await client.post(f"/leave/requests/{approved['id']}/cancel", json={}, headers=staff)
    assert response.json()["status"] == "cancelled"

    balance = await balance_of(client, staff, "CUTI_TAHUNAN", year=year)
    assert (balance["pending"], balance["used"], balance["available"]) == (0, 0, 12)

    twice = await client.post(f"/leave/requests/{approved['id']}/cancel", json={}, headers=staff)
    assert twice.status_code == 422
    assert twice.json()["detail"]["code"] == "cannot_cancel"

    # Tanggal yang dibatalkan boleh diajukan lagi.
    await _submit_ok(client, org, "staff", weeks=2)


async def test_cannot_cancel_started_leave(client: AsyncClient, org: LeaveOrg) -> None:
    last_monday = monday_after(0) - timedelta(days=7)
    response = await submit(client, org.headers["staff"], org.sick["id"], last_monday, last_monday)
    request = response.json()
    await decide(client, org.headers["manager"], request["id"], "approved")

    cancel = await client.post(
        f"/leave/requests/{request['id']}/cancel", json={}, headers=org.headers["staff"]
    )
    assert cancel.status_code == 422
    assert cancel.json()["detail"]["code"] == "cannot_cancel"


async def test_cancel_authorization(client: AsyncClient, org: LeaveOrg) -> None:
    request = await _submit_ok(client, org, "staff")
    url = f"/leave/requests/{request['id']}/cancel"

    by_manager = await client.post(url, json={}, headers=org.headers["manager"])
    assert by_manager.status_code == 403
    assert by_manager.json()["detail"]["code"] == "cannot_cancel_others"
    assert (await client.post(url, json={}, headers=org.headers["peer"])).status_code == 404

    by_hr = await client.post(url, json={}, headers=org.hr)
    assert by_hr.json()["status"] == "cancelled"


# --- Baca ----------------------------------------------------------------------------


async def test_request_visibility(client: AsyncClient, org: LeaveOrg) -> None:
    request = await _submit_ok(client, org, "staff")
    url = f"/leave/requests/{request['id']}"
    for who in ("staff", "manager"):
        assert (await client.get(url, headers=org.headers[who])).status_code == 200, who
    assert (await client.get(url, headers=org.hr)).status_code == 200
    for who in ("peer", "director"):
        assert (await client.get(url, headers=org.headers[who])).status_code == 404, who


async def test_list_scopes_filters_and_pagination(client: AsyncClient, org: LeaveOrg) -> None:
    first = await _submit_ok(client, org, "staff", weeks=2)
    second = await _submit_ok(client, org, "staff", weeks=3)
    peer = await _submit_ok(client, org, "peer", weeks=2)
    await decide(client, org.headers["manager"], first["id"], "approved")
    year = monday_after(14).year
    staff = org.headers["staff"]

    mine = await client.get("/leave/requests", params={"year": year}, headers=staff)
    assert [i["id"] for i in mine.json()["items"]] == [second["id"], first["id"]]  # terbaru dulu

    page_one = await client.get("/leave/requests", params={"year": year, "limit": 1}, headers=staff)
    cursor = page_one.json()["next_cursor"]
    assert cursor is not None
    page_two = await client.get(
        "/leave/requests", params={"year": year, "limit": 1, "cursor": cursor}, headers=staff
    )
    assert [i["id"] for i in page_two.json()["items"]] == [first["id"]]

    approved = await client.get(
        "/leave/requests", params={"year": year, "status": "approved"}, headers=staff
    )
    assert [i["id"] for i in approved.json()["items"]] == [first["id"]]

    other_year = await client.get("/leave/requests", params={"year": year - 1}, headers=staff)
    assert other_year.json()["items"] == []

    assert await _approvals(client, org.headers["manager"]) == [second["id"], peer["id"]]
    assert await _approvals(client, staff) == []

    everything = await client.get(
        "/leave/requests", params={"scope": "all", "year": year}, headers=org.hr
    )
    assert {i["id"] for i in everything.json()["items"]} == {first["id"], second["id"], peer["id"]}
    for who in ("staff", "manager"):
        forbidden = await client.get(
            "/leave/requests", params={"scope": "all"}, headers=org.headers[who]
        )
        assert forbidden.status_code == 403, who


async def test_team_calendar(
    client: AsyncClient, org: LeaveOrg, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    staff_request = await _submit_ok(client, org, "staff", weeks=2)
    peer_request = await _submit_ok(client, org, "peer", weeks=2)
    rejected = await _submit_ok(client, org, "peer", weeks=3)
    await decide(client, org.headers["manager"], rejected["id"], "rejected")
    manager_request = await _submit_ok(client, org, "manager", weeks=2)

    start = monday_after(14)
    params = {"start": start.isoformat(), "end": (start + timedelta(days=30)).isoformat()}

    async def calendar(headers: dict[str, str], **extra: str) -> list[str]:
        response = await client.get("/leave/team-calendar", params=params | extra, headers=headers)
        assert response.status_code == 200, response.text
        return sorted(item["request_id"] for item in response.json()["items"])

    assert await calendar(org.headers["manager"]) == sorted(
        [staff_request["id"], peer_request["id"]]
    )
    assert await calendar(org.headers["director"]) == [manager_request["id"]]
    assert await calendar(org.hr) == sorted(
        [staff_request["id"], peer_request["id"], manager_request["id"]]
    )
    assert await calendar(org.hr, org_unit_id=org.unit_id) == await calendar(org.hr)

    employee = await client.get("/leave/team-calendar", params=params, headers=org.headers["staff"])
    assert employee.status_code == 403

    too_long = await client.get(
        "/leave/team-calendar",
        params={"start": start.isoformat(), "end": (start + timedelta(days=93)).isoformat()},
        headers=org.hr,
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["code"] == "invalid_calendar_range"

    other_hr = auth_headers(await make_tenant(), Role.HR_ADMIN)
    assert await calendar(other_hr) == []


# --- Isolasi tenant ------------------------------------------------------------------


async def test_tenant_isolation(
    client: AsyncClient, org: LeaveOrg, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> None:
    request = await _submit_ok(client, org, "staff")
    tenant_b = await make_tenant()
    hr_b = auth_headers(tenant_b, Role.HR_ADMIN)
    employee_b = auth_headers(tenant_b, Role.EMPLOYEE, employee_id=UUID(org.ids["staff"]))
    year = str(monday_after(14).year)

    assert (await client.get(f"/leave/requests/{request['id']}", headers=hr_b)).status_code == 404
    assert (await decide(client, hr_b, request["id"], "approved")).status_code == 404
    cancel = await client.post(f"/leave/requests/{request['id']}/cancel", json={}, headers=hr_b)
    assert cancel.status_code == 404

    everything = await client.get(
        "/leave/requests", params={"scope": "all", "year": year}, headers=hr_b
    )
    assert everything.json()["items"] == []
    assert await _approvals(client, hr_b) == []

    balance = await client.get(
        "/leave/balances", params={"employee_id": org.ids["staff"]}, headers=hr_b
    )
    assert balance.status_code == 404

    start = monday_after(21)
    body = {
        "leave_type_id": org.annual["id"],
        "start_date": start.isoformat(),
        "end_date": start.isoformat(),
    }
    # Token tenant B yang (sengaja) membawa employee_id tenant A tetap tidak bisa apa-apa.
    for url in ("/leave/requests/validate", "/leave/requests"):
        response = await client.post(url, json=body, headers=employee_b)
        assert response.status_code == 404, url
    mine = await client.get("/leave/requests", params={"year": year}, headers=employee_b)
    assert mine.json()["items"] == []

    # Data tenant A tidak berubah.
    staff_balance = await balance_of(client, org.headers["staff"], "CUTI_TAHUNAN", year=year)
    assert staff_balance["pending"] == 3
