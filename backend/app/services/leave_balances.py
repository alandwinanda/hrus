"""Saldo cuti. Disimpan per karyawan/tipe/tahun dan diubah di transaksi yang sama dengan
pengajuan, approval, dan pembatalan. Tidak pernah dihitung ulang dari histori pengajuan.

Baris saldo dibuat saat pertama dibutuhkan, dengan jatah dari policy per awal tahun (atau per
tanggal masuk kalau masuk di tahun itu). Top-up saat ulang tahun kerja dan carry-over
dikerjakan job accrual (menyusul).
"""

from datetime import date
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, RuleViolationError
from app.core.security import AccessClaims
from app.models import Employee, LeaveBalance, LeavePolicy, LeaveType
from app.schemas.leave import BalanceAdjustment, LeaveBalanceRead, LeaveBalanceSummary
from app.services import employees
from app.services.audit import record_audit
from app.services.leave_config import get_leave_type
from app.services.tenant import tenant_today


def service_months(hire_date: date, as_of: date) -> int:
    months = (as_of.year - hire_date.year) * 12 + (as_of.month - hire_date.month)
    if as_of.day < hire_date.day:
        months -= 1
    return max(months, 0)


async def entitlement_for(
    session: AsyncSession, tenant_id: UUID, employee: Employee, leave_type_id: UUID, year: int
) -> int:
    """Jatah dari policy paling spesifik: grade yang cocok dulu, lalu masa kerja tertinggi."""
    reference = max(date(year, 1, 1), employee.hire_date)
    job = await employees.job_as_of(session, tenant_id, employee.id, reference)
    grade = job.grade.upper() if job else None
    months = service_months(employee.hire_date, reference)
    annual_days = await session.scalar(
        select(LeavePolicy.annual_days)
        .where(
            LeavePolicy.tenant_id == tenant_id,
            LeavePolicy.leave_type_id == leave_type_id,
            LeavePolicy.min_service_months <= months,
            or_(LeavePolicy.grade == grade, LeavePolicy.grade.is_(None)),
        )
        .order_by(LeavePolicy.grade.is_(None), LeavePolicy.min_service_months.desc())
        .limit(1)
    )
    return annual_days or 0


async def get_balance(
    session: AsyncSession,
    tenant_id: UUID,
    employee_id: UUID,
    leave_type: LeaveType,
    year: int,
    *,
    lock: bool,
) -> LeaveBalance:
    """Ambil saldo (buat dulu kalau belum ada). lock=True untuk perubahan saldo: FOR UPDATE
    membuat pengajuan/approval bersamaan untuk karyawan yang sama diproses bergantian."""
    stmt = select(LeaveBalance).where(
        LeaveBalance.tenant_id == tenant_id,
        LeaveBalance.employee_id == employee_id,
        LeaveBalance.leave_type_id == leave_type.id,
        LeaveBalance.year == year,
    )
    if lock:
        stmt = stmt.with_for_update()
    balance = await session.scalar(stmt)
    if balance is not None:
        return balance

    employee = await session.scalar(
        select(Employee).where(Employee.tenant_id == tenant_id, Employee.id == employee_id)
    )
    if employee is None:
        raise NotFoundError("Karyawan tidak ditemukan.")
    entitled = await entitlement_for(session, tenant_id, employee, leave_type.id, year)
    created_id = await session.scalar(
        insert(LeaveBalance)
        .values(
            tenant_id=tenant_id,
            employee_id=employee_id,
            leave_type_id=leave_type.id,
            year=year,
            entitled=entitled,
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "employee_id", "leave_type_id", "year"]
        )
        .returning(LeaveBalance.id)
    )
    if created_id is not None:
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            action="leave_balance.init",
            entity_type="leave_balance",
            entity_id=created_id,
            after={"leave_type": leave_type.code, "year": year, "entitled": entitled},
        )
    balance = await session.scalar(stmt.execution_options(populate_existing=True))
    if balance is None:  # pragma: no cover - baris pasti ada setelah insert/konflik
        raise NotFoundError("Saldo cuti tidak ditemukan.")
    return balance


def to_read(balance: LeaveBalance, leave_type: LeaveType) -> LeaveBalanceRead:
    return LeaveBalanceRead(
        leave_type_id=leave_type.id,
        leave_type_code=leave_type.code,
        leave_type_name=leave_type.name,
        year=balance.year,
        entitled=balance.entitled,
        carried_over=balance.carried_over,
        adjusted=balance.adjusted,
        used=balance.used,
        pending=balance.pending,
        available=balance.available,
    )


async def list_balances(
    session: AsyncSession,
    viewer: AccessClaims,
    *,
    employee_id: UUID | None,
    year: int | None,
) -> LeaveBalanceSummary:
    """Saldo semua tipe cuti bersaldo. HR: siapa saja, karyawan: dirinya, atasan: bawahan."""
    target = employee_id or viewer.employee_id
    if target is None:
        raise RuleViolationError(
            "Akun ini belum terhubung dengan data karyawan.", code="no_employee_profile"
        )
    await employees.get_employee(session, viewer, target)  # 404 kalau tidak boleh melihat
    year = year or (await tenant_today(session, viewer.tenant_id)).year

    leave_types = await session.scalars(
        select(LeaveType)
        .where(
            LeaveType.tenant_id == viewer.tenant_id,
            LeaveType.is_active.is_(True),
            LeaveType.requires_balance.is_(True),
        )
        .order_by(LeaveType.code)
    )
    items = []
    for leave_type in leave_types.all():
        balance = await get_balance(session, viewer.tenant_id, target, leave_type, year, lock=False)
        items.append(to_read(balance, leave_type))
    return LeaveBalanceSummary(employee_id=target, year=year, items=items)


async def adjust_balance(
    session: AsyncSession, actor: AccessClaims, data: BalanceAdjustment
) -> LeaveBalanceRead:
    leave_type = await get_leave_type(session, actor.tenant_id, data.leave_type_id)
    if not leave_type.requires_balance:
        raise RuleViolationError(
            "Tipe cuti ini tidak memakai saldo.", code="leave_type_without_balance"
        )
    balance = await get_balance(
        session, actor.tenant_id, data.employee_id, leave_type, data.year, lock=True
    )
    before = to_read(balance, leave_type)
    if balance.available + data.delta < 0:
        raise RuleViolationError(
            f"Koreksi membuat saldo minus (tersedia {balance.available} hari).",
            code="balance_negative",
        )
    balance.adjusted += data.delta
    await session.flush()
    after = to_read(balance, leave_type)
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="leave_balance.adjust",
        entity_type="leave_balance",
        entity_id=balance.id,
        before=before.model_dump(mode="json"),
        after=after.model_dump(mode="json") | {"delta": data.delta, "note": data.note},
    )
    return after
