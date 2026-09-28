"""Job cuti: accrual harian dan hangus carry-over. Logic bisnis di app/services/leave_accrual.py."""

from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RuleViolationError
from app.jobs.registry import ChunkContext, ChunkResult, JobDefinition
from app.models import Employee
from app.services import leave_accrual
from app.services.tenant import tenant_today

CHUNK_SIZE = 500


class LeaveJobParams(BaseModel):
    as_of: date | None = None


async def _prepare(
    session: AsyncSession, tenant_id: UUID, params: LeaveJobParams
) -> LeaveJobParams:
    today = await tenant_today(session, tenant_id)
    as_of = params.as_of or today
    if as_of > today:
        raise RuleViolationError("as_of tidak boleh di masa depan.", code="as_of_in_future")
    return LeaveJobParams(as_of=as_of)


async def _plan(
    session: AsyncSession, tenant_id: UUID, params: LeaveJobParams
) -> list[dict[str, Any]]:
    """Karyawan yang sudah mulai bekerja per as_of, dipecah per CHUNK_SIZE berdasarkan id."""
    ids = list(
        await session.scalars(
            select(Employee.id)
            .where(Employee.tenant_id == tenant_id, Employee.hire_date <= params.as_of)
            .order_by(Employee.id)
        )
    )
    return [
        {"first_id": str(group[0]), "last_id": str(group[-1]), "employees": len(group)}
        for group in (ids[i : i + CHUNK_SIZE] for i in range(0, len(ids), CHUNK_SIZE))
    ]


def _bounds(ctx: ChunkContext) -> dict[str, Any]:
    return {
        "first_id": UUID(ctx.chunk["first_id"]),
        "last_id": UUID(ctx.chunk["last_id"]),
        "as_of": ctx.params.as_of,
        "actor_user_id": ctx.actor_user_id,
        "job_run_id": ctx.run_id,
    }


async def _accrue(session: AsyncSession, ctx: ChunkContext) -> ChunkResult:
    return await leave_accrual.accrue(session, ctx.tenant_id, **_bounds(ctx))


async def _expire(session: AsyncSession, ctx: ChunkContext) -> ChunkResult:
    return await leave_accrual.expire_carry_over(session, ctx.tenant_id, **_bounds(ctx))


ACCRUAL = JobDefinition(
    code="leave_accrual",
    description=(
        "Buat saldo cuti tahun berjalan, naikkan jatah saat ulang tahun kerja atau promosi, "
        "dan terapkan carry-over dari tahun lalu."
    ),
    params_model=LeaveJobParams,
    prepare=_prepare,
    plan=_plan,
    run_chunk=_accrue,
    default_cron="0 1 * * *",
)

CARRY_OVER_EXPIRY = JobDefinition(
    code="leave_carry_over_expiry",
    description="Hanguskan sisa carry-over cuti yang sudah melewati tanggal kedaluwarsa.",
    params_model=LeaveJobParams,
    prepare=_prepare,
    plan=_plan,
    run_chunk=_expire,
    default_cron="30 1 * * *",
)
