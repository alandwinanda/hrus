import dataclasses
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs import leave as leave_jobs
from app.jobs.definitions import DEFINITIONS
from app.jobs.dispatch import TASK_RUN_CHUNK, TASK_START_RUN
from app.jobs.registry import ChunkContext, ChunkResult
from app.models import AuditLog, LeaveBalance, Role
from tests.conftest import AuthHeaders, MakeTenant
from tests.core_hr_helpers import create_employee, create_org_unit
from tests.job_helpers import InlineDispatch, run_job
from tests.leave_helpers import create_leave_type, create_policy, tenant_today

ACCRUAL = "leave_accrual"


@pytest.fixture
async def setup(
    client: AsyncClient, make_tenant: MakeTenant, auth_headers: AuthHeaders
) -> dict[str, Any]:
    """Tenant dengan 3 karyawan, tipe cuti tahunan, dan policy 12 hari."""
    tenant = await make_tenant()
    hr = auth_headers(tenant, Role.HR_ADMIN)
    unit = await create_org_unit(client, hr)
    for _ in range(3):
        await create_employee(client, hr, unit["id"])
    annual = await create_leave_type(client, hr, code="CUTI_TAHUNAN")
    await create_policy(client, hr, annual["id"], annual_days=12, min_service_months=12)
    return {"tenant": tenant, "hr": hr}


async def _balance_count(sessions: async_sessionmaker[AsyncSession], setup: dict[str, Any]) -> int:
    async with sessions() as s:
        return int(
            await s.scalar(
                select(func.count())
                .select_from(LeaveBalance)
                .where(LeaveBalance.tenant_id == setup["tenant"].id)
            )
            or 0
        )


# --- Definisi dan pembuatan run ----------------------------------------------------------


async def test_definitions(client: AsyncClient, setup: dict[str, Any]) -> None:
    response = await client.get("/jobs/definitions", headers=setup["hr"])
    assert response.status_code == 200
    codes = {d["code"]: d for d in response.json()}
    assert set(codes) == {"leave_accrual", "leave_carry_over_expiry"}
    assert codes[ACCRUAL]["supports_dry_run"] is True
    assert "as_of" in codes[ACCRUAL]["params_schema"]["properties"]


async def test_dry_run_then_real_run(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hr = setup["hr"]
    response = await client.post(
        "/jobs/runs", json={"job_code": ACCRUAL, "dry_run": True}, headers=hr
    )
    assert response.status_code == 202, response.text
    queued = response.json()
    assert queued["status"] == "queued"
    assert queued["trigger"] == "user"
    assert queued["params"] == {"as_of": tenant_today().isoformat()}
    assert [t.name for t in dispatcher.sent] == [TASK_START_RUN]

    await dispatcher.drain()
    dry = (await client.get(f"/jobs/runs/{queued['id']}", headers=hr)).json()
    assert dry["status"] == "success"
    assert (dry["chunks_total"], dry["chunks_done"], dry["chunks_failed"]) == (1, 1, 0)
    assert dry["output"] == {"counts": {"balance_created": 3}}
    assert await _balance_count(admin_sessionmaker, setup) == 0  # dry-run tidak mengubah data

    chunks = (await client.get(f"/jobs/runs/{queued['id']}/chunks", headers=hr)).json()
    changes = chunks["items"][0]["output"]["changes"]
    assert [c["kind"] for c in changes] == ["balance_created"] * 3
    assert {c["entitled"] for c in changes} == {12}

    real = await run_job(client, hr, dispatcher, ACCRUAL)
    assert real["output"] == {"counts": {"balance_created": 3}}
    assert await _balance_count(admin_sessionmaker, setup) == 3

    again = await run_job(client, hr, dispatcher, ACCRUAL)
    assert again["status"] == "success"
    assert again["output"] == {"counts": {}}  # idempotent

    logs = (await client.get(f"/jobs/runs/{real['id']}/logs", headers=hr)).json()["items"]
    assert [entry["message"] for entry in logs] == [
        "Run dibuat.",
        "Mulai: 1 chunk.",
        "Selesai: success (1 sukses, 0 gagal).",
    ]


async def test_create_run_validation(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch
) -> None:
    hr = setup["hr"]
    cases = [
        ({"job_code": "tidak_ada"}, 404, "job_not_found"),
        ({"job_code": ACCRUAL, "params": {"as_of": "bukan-tanggal"}}, 422, "invalid_job_params"),
        ({"job_code": ACCRUAL, "params": {"as_of": "2999-01-01"}}, 422, "as_of_in_future"),
    ]
    for body, status_code, code in cases:
        response = await client.post("/jobs/runs", json=body, headers=hr)
        assert response.status_code == status_code, response.text
        assert response.json()["detail"]["code"] == code
    assert dispatcher.sent == []


async def test_one_active_run_per_job_and_idempotency(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch
) -> None:
    hr = setup["hr"]
    key = {"Idempotency-Key": "chat-job-0001"}
    first = await client.post("/jobs/runs", json={"job_code": ACCRUAL}, headers=hr | key)
    same = await client.post("/jobs/runs", json={"job_code": ACCRUAL}, headers=hr | key)
    assert first.status_code == same.status_code == 202
    assert first.json()["id"] == same.json()["id"]

    conflict = await client.post("/jobs/runs", json={"job_code": ACCRUAL}, headers=hr)
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "job_already_running"

    # Job lain tetap boleh jalan bersamaan.
    other = await client.post(
        "/jobs/runs", json={"job_code": "leave_carry_over_expiry"}, headers=hr
    )
    assert other.status_code == 202
    await dispatcher.drain()


async def test_cancel(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch
) -> None:
    hr = setup["hr"]
    run = (await client.post("/jobs/runs", json={"job_code": ACCRUAL}, headers=hr)).json()
    cancelled = await client.post(f"/jobs/runs/{run['id']}/cancel", headers=hr)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"

    await dispatcher.drain()  # start_run tidak melakukan apa-apa untuk run yang dibatalkan
    final = (await client.get(f"/jobs/runs/{run['id']}", headers=hr)).json()
    assert (final["status"], final["chunks_total"]) == ("cancelled", 0)

    again = await client.post(f"/jobs/runs/{run['id']}/cancel", headers=hr)
    assert again.status_code == 422
    assert again.json()["detail"]["code"] == "job_not_active"


# --- Chunk, gagal, dan restart -----------------------------------------------------------


async def test_failed_chunk_is_retried_then_restartable(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    monkeypatch: pytest.MonkeyPatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hr = setup["hr"]
    monkeypatch.setattr(leave_jobs, "CHUNK_SIZE", 1)
    original = DEFINITIONS[ACCRUAL]
    attempts: list[int] = []

    async def flaky(session: AsyncSession, ctx: ChunkContext) -> ChunkResult:
        if ctx.chunk_no == 2:
            attempts.append(ctx.chunk_no)
            raise RuntimeError("database sibuk")
        return await original.run_chunk(session, ctx)

    monkeypatch.setitem(DEFINITIONS, ACCRUAL, dataclasses.replace(original, run_chunk=flaky))
    run = await run_job(client, hr, dispatcher, ACCRUAL)
    assert run["status"] == "partial"
    assert (run["chunks_total"], run["chunks_done"], run["chunks_failed"]) == (3, 2, 1)
    assert run["output"] == {"counts": {"balance_created": 2}}
    assert len(attempts) == original.max_attempts

    chunks = (await client.get(f"/jobs/runs/{run['id']}/chunks", headers=hr)).json()["items"]
    failed = [c for c in chunks if c["status"] == "failed"]
    assert [(c["chunk_no"], c["attempts"]) for c in failed] == [(2, original.max_attempts)]
    assert failed[0]["error"] == "RuntimeError: database sibuk"
    assert await _balance_count(admin_sessionmaker, setup) == 2  # chunk sukses sudah commit

    # Setelah masalahnya beres, restart hanya mengerjakan chunk yang gagal.
    monkeypatch.setitem(DEFINITIONS, ACCRUAL, original)
    dispatcher.sent.clear()
    retried = await client.post(f"/jobs/runs/{run['id']}/retry", headers=hr)
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "running"
    assert [t.name for t in dispatcher.sent] == [TASK_RUN_CHUNK]
    await dispatcher.drain()

    final = (await client.get(f"/jobs/runs/{run['id']}", headers=hr)).json()
    assert final["status"] == "success"
    assert (final["chunks_done"], final["chunks_failed"]) == (3, 0)
    assert final["output"] == {"counts": {"balance_created": 3}}
    assert await _balance_count(admin_sessionmaker, setup) == 3

    not_retryable = await client.post(f"/jobs/runs/{run['id']}/retry", headers=hr)
    assert not_retryable.json()["detail"]["code"] == "job_not_retryable"


async def test_chunk_is_not_executed_twice(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Task chunk yang terkirim dua kali (redelivery) tidak mengulang perubahan."""
    run = await run_job(client, setup["hr"], dispatcher, ACCRUAL)
    chunk_task = next(t for t in dispatcher.sent if t.name == TASK_RUN_CHUNK)
    dispatcher(chunk_task)
    assert await dispatcher.drain() == 1
    async with admin_sessionmaker() as s:
        audits = await s.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.tenant_id == setup["tenant"].id,
                AuditLog.action == "leave_balance.accrual",
            )
        )
    assert audits == 3
    assert run["status"] == "success"


async def test_plan_failure_marks_run_failed(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = DEFINITIONS[ACCRUAL]

    async def broken_plan(*_: Any) -> list[dict[str, Any]]:
        raise ValueError("data rusak")

    monkeypatch.setitem(DEFINITIONS, ACCRUAL, dataclasses.replace(original, plan=broken_plan))
    run = await run_job(client, setup["hr"], dispatcher, ACCRUAL)
    assert (run["status"], run["error"]) == ("failed", "ValueError: data rusak")

    monkeypatch.setitem(DEFINITIONS, ACCRUAL, original)
    retried = await client.post(f"/jobs/runs/{run['id']}/retry", headers=setup["hr"])
    assert retried.json()["status"] == "queued"
    await dispatcher.drain()
    final = (await client.get(f"/jobs/runs/{run['id']}", headers=setup["hr"])).json()
    assert final["status"] == "success"


# --- Daftar, jadwal, otorisasi, isolasi -------------------------------------------------


async def test_list_runs(
    client: AsyncClient, setup: dict[str, Any], dispatcher: InlineDispatch
) -> None:
    hr = setup["hr"]
    first = await run_job(client, hr, dispatcher, ACCRUAL, dry_run=True)
    second = await run_job(client, hr, dispatcher, ACCRUAL)
    listed = await client.get("/jobs/runs", params={"job_code": ACCRUAL, "limit": 1}, headers=hr)
    assert [r["id"] for r in listed.json()["items"]] == [second["id"]]
    cursor = listed.json()["next_cursor"]
    page_two = await client.get(
        "/jobs/runs", params={"job_code": ACCRUAL, "limit": 1, "cursor": cursor}, headers=hr
    )
    assert [r["id"] for r in page_two.json()["items"]] == [first["id"]]
    failed_only = await client.get("/jobs/runs", params={"status": "failed"}, headers=hr)
    assert failed_only.json()["items"] == []


async def test_schedules(
    client: AsyncClient,
    setup: dict[str, Any],
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    hr = setup["hr"]
    listed = (await client.get("/jobs/schedules", headers=hr)).json()["items"]
    assert {(s["job_code"], s["cron"], s["timezone"], s["is_active"]) for s in listed} == {
        ("leave_accrual", "0 1 * * *", "Asia/Jakarta", True),
        ("leave_carry_over_expiry", "30 1 * * *", "Asia/Jakarta", True),
    }
    schedule = next(s for s in listed if s["job_code"] == ACCRUAL)

    invalid = await client.patch(
        f"/jobs/schedules/{schedule['id']}", json={"cron": "tiap hari"}, headers=hr
    )
    assert invalid.status_code == 422
    bad_zone = await client.patch(
        f"/jobs/schedules/{schedule['id']}", json={"timezone": "Mars/Olympus"}, headers=hr
    )
    assert bad_zone.status_code == 422
    bad_params = await client.patch(
        f"/jobs/schedules/{schedule['id']}", json={"params": {"as_of": "x"}}, headers=hr
    )
    assert bad_params.json()["detail"]["code"] == "invalid_job_params"

    updated = await client.patch(
        f"/jobs/schedules/{schedule['id']}",
        json={"cron": "15 2 * * *", "timezone": "Asia/Makassar", "is_active": False},
        headers=hr,
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert (body["cron"], body["timezone"], body["is_active"]) == (
        "15 2 * * *",
        "Asia/Makassar",
        False,
    )
    assert body["next_run_at"].endswith(":15:00Z")  # 02:15 WITA = 18:15 UTC

    async with admin_sessionmaker() as s:
        audit = await s.scalar(select(AuditLog.action).where(AuditLog.entity_id == schedule["id"]))
    assert audit == "job_schedule.update"


async def test_only_hr_and_tenant_isolation(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    make_tenant: MakeTenant,
    auth_headers: AuthHeaders,
) -> None:
    run = await run_job(client, setup["hr"], dispatcher, ACCRUAL)
    schedule = (await client.get("/jobs/schedules", headers=setup["hr"])).json()["items"][0]
    run_url = f"/jobs/runs/{run['id']}"

    for role in (Role.EMPLOYEE, Role.MANAGER):
        headers = auth_headers(setup["tenant"], role)
        for method, url in (
            ("GET", "/jobs/definitions"),
            ("POST", "/jobs/runs"),
            ("GET", "/jobs/runs"),
            ("GET", run_url),
            ("POST", f"{run_url}/cancel"),
            ("GET", "/jobs/schedules"),
        ):
            body = {"job_code": ACCRUAL} if method == "POST" else None
            response = await client.request(method, url, json=body, headers=headers)
            assert response.status_code == 403, (role, method, url)

    other_hr = auth_headers(await make_tenant(), Role.HR_ADMIN)
    for url in (run_url, f"{run_url}/chunks", f"{run_url}/logs"):
        assert (await client.get(url, headers=other_hr)).status_code == 404, url
    assert (await client.post(f"{run_url}/retry", headers=other_hr)).status_code == 404
    listed = await client.get("/jobs/runs", headers=other_hr)
    assert listed.json()["items"] == []
    patched = await client.patch(
        f"/jobs/schedules/{schedule['id']}", json={"is_active": False}, headers=other_hr
    )
    assert patched.status_code == 404
