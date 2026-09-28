from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import get_sessionmaker
from app.jobs import runner, scheduler
from app.jobs.dispatch import TASK_RUN_CHUNK, TASK_START_RUN
from app.models import JobRun, JobRunChunk, JobSchedule, Role
from tests.conftest import AuthHeaders, MakeTenant
from tests.core_hr_helpers import create_employee, create_org_unit
from tests.job_helpers import InlineDispatch

ACCRUAL = "leave_accrual"


@pytest.fixture
async def setup(make_tenant: MakeTenant, auth_headers: AuthHeaders) -> dict[str, Any]:
    tenant = await make_tenant()
    return {"tenant": tenant, "hr": auth_headers(tenant, Role.HR_ADMIN)}


async def _make_due(
    sessions: async_sessionmaker[AsyncSession], tenant_id: UUID, job_code: str = ACCRUAL
) -> UUID:
    async with sessions() as s, s.begin():
        schedule_id = await s.scalar(
            update(JobSchedule)
            .where(JobSchedule.tenant_id == tenant_id, JobSchedule.job_code == job_code)
            .values(next_run_at=datetime.now(UTC) - timedelta(minutes=1))
            .returning(JobSchedule.id)
        )
    assert schedule_id is not None
    return schedule_id


async def _schedule(sessions: async_sessionmaker[AsyncSession], schedule_id: UUID) -> JobSchedule:
    async with sessions() as s:
        schedule = await s.get(JobSchedule, schedule_id)
    assert schedule is not None
    return schedule


async def test_due_schedule_creates_run_once(
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    tenant_id = setup["tenant"].id
    schedule_id = await _make_due(admin_sessionmaker, tenant_id)

    result = await scheduler.tick(get_sessionmaker(), dispatcher)
    assert result.leader is True
    async with admin_sessionmaker() as s:
        runs = list(await s.scalars(select(JobRun).where(JobRun.schedule_id == schedule_id)))
    assert [(r.trigger, r.status, r.job_code) for r in runs] == [("schedule", "queued", ACCRUAL)]
    assert runs[0].id in result.created
    assert (TASK_START_RUN, (str(tenant_id), str(runs[0].id))) in [
        (t.name, t.args) for t in dispatcher.sent
    ]

    schedule = await _schedule(admin_sessionmaker, schedule_id)
    assert schedule.last_run_at is not None
    assert schedule.next_run_at > datetime.now(UTC)
    # 01:00 WIB = 18:00 UTC hari sebelumnya.
    assert (schedule.next_run_at.hour, schedule.next_run_at.minute) == (18, 0)

    again = await scheduler.tick(get_sessionmaker(), dispatcher)
    assert runs[0].id not in again.created
    await dispatcher.drain()


async def test_schedule_skipped_while_previous_run_active(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    active = await client.post("/jobs/runs", json={"job_code": ACCRUAL}, headers=setup["hr"])
    assert active.status_code == 202
    schedule_id = await _make_due(admin_sessionmaker, setup["tenant"].id)

    result = await scheduler.tick(get_sessionmaker(), dispatcher)
    assert schedule_id in result.skipped
    # Tetap maju ke jadwal berikutnya, tidak menumpuk run.
    assert (await _schedule(admin_sessionmaker, schedule_id)).next_run_at > datetime.now(UTC)
    await dispatcher.drain()


async def test_inactive_schedule_is_ignored(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    schedule_id = await _make_due(admin_sessionmaker, setup["tenant"].id)
    await client.patch(
        f"/jobs/schedules/{schedule_id}", json={"is_active": False}, headers=setup["hr"]
    )
    result = await scheduler.tick(get_sessionmaker(), dispatcher)
    assert schedule_id not in result.skipped
    async with admin_sessionmaker() as s:
        run = await s.scalar(select(JobRun.id).where(JobRun.schedule_id == schedule_id))
    assert run is None


async def test_lost_tasks_are_resent(
    client: AsyncClient,
    setup: dict[str, Any],
    dispatcher: InlineDispatch,
    admin_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Task hilang di broker: run antre dan chunk pending dikirim ulang oleh scheduler."""
    hr = setup["hr"]
    unit = await create_org_unit(client, hr)
    await create_employee(client, hr, unit["id"])
    queued = (await client.post("/jobs/runs", json={"job_code": ACCRUAL}, headers=hr)).json()
    dispatcher.queue.clear()  # anggap task start_run hilang
    stale = datetime.now(UTC) - timedelta(hours=1)
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(update(JobRun).where(JobRun.id == queued["id"]).values(updated_at=stale))

    result = await scheduler.tick(get_sessionmaker(), dispatcher)
    assert UUID(queued["id"]) in result.requeued
    assert dispatcher.queue[-1].name == TASK_START_RUN

    # start_run jalan, tapi semua task chunk-nya hilang.
    dispatcher.queue.clear()
    await runner.start_run(
        get_sessionmaker(), lambda _: None, setup["tenant"].id, UUID(queued["id"])
    )
    async with admin_sessionmaker() as s, s.begin():
        await s.execute(update(JobRun).where(JobRun.id == queued["id"]).values(updated_at=stale))

    resumed = await scheduler.tick(get_sessionmaker(), dispatcher)
    assert UUID(queued["id"]) in resumed.resumed
    assert [t.name for t in dispatcher.queue] == [TASK_RUN_CHUNK]
    await dispatcher.drain()
    final = (await client.get(f"/jobs/runs/{queued['id']}", headers=hr)).json()
    assert final["status"] == "success"
    async with admin_sessionmaker() as s:
        pending = await s.scalar(
            select(JobRunChunk.id).where(
                JobRunChunk.job_run_id == queued["id"], JobRunChunk.status == "pending"
            )
        )
    assert pending is None


async def test_only_one_scheduler_ticks_at_a_time(dispatcher: InlineDispatch) -> None:
    sessions = get_sessionmaker()
    async with sessions() as holder, holder.begin():
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": scheduler.SCHEDULER_LOCK_KEY}
        )
        result = await scheduler.tick(sessions, dispatcher)
    assert result.leader is False
    assert dispatcher.sent == []


async def test_scheduler_function_is_not_public(session: AsyncSession) -> None:
    """Fungsi SECURITY DEFINER hanya boleh dieksekusi role aplikasi, bukan PUBLIC."""
    public = await session.scalar(
        text(
            "SELECT has_function_privilege('public', "
            "'job_scheduler_work(timestamptz, timestamptz, timestamptz, integer)', 'EXECUTE')"
        )
    )
    assert public is False
