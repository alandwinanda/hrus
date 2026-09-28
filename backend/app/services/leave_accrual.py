"""Accrual saldo cuti dan hangus carry-over (ADR 010). Dipanggil job per chunk karyawan.

Semua query per chunk (bukan per karyawan), dan tiap langkah idempotent: menjalankan ulang
untuk tanggal yang sama tidak mengubah apa-apa.

- Saldo tahun berjalan dibuat untuk karyawan aktif yang belum punya.
- Jatah naik kalau policy per tanggal as_of lebih besar (ulang tahun kerja, promosi, policy
  diubah HR). Jatah tidak pernah diturunkan otomatis.
- Carry-over dari sisa saldo tahun lalu diterapkan sekali per tahun, dibatasi policy.
- Carry-over yang belum terpakai hangus di tanggal kedaluwarsanya (carry-over dipakai duluan).
"""

from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, and_, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs.registry import ChunkResult
from app.models import Employee, EmployeeJob, EmploymentStatus, LeaveBalance, LeaveType
from app.rules import leave as rules
from app.services.audit import record_audit
from app.services.leave_balances import load_policies


def _job_at(as_of: Any, prefix: str) -> Any:
    """Kolom grade + status jabatan yang berlaku per tanggal (tanggal boleh ekspresi SQL)."""
    return (
        select(
            EmployeeJob.grade.label(f"{prefix}_grade"),
            EmployeeJob.employment_status.label(f"{prefix}_status"),
        )
        .where(
            EmployeeJob.tenant_id == Employee.tenant_id,
            EmployeeJob.employee_id == Employee.id,
            EmployeeJob.effdt <= as_of,
        )
        .order_by(EmployeeJob.effdt.desc(), EmployeeJob.effseq.desc())
        .limit(1)
        .lateral(f"{prefix}_job")
    )


def _chunk_employees(tenant_id: UUID, first_id: UUID, last_id: UUID, as_of: date) -> Select[Any]:
    """Karyawan di rentang id + jabatan per as_of dan per tanggal acuan jatah awal tahun."""
    current = _job_at(as_of, "cur")
    reference = _job_at(func.greatest(date(as_of.year, 1, 1), Employee.hire_date), "ref")
    return (
        select(Employee.id, Employee.hire_date, *current.c, *reference.c)
        .select_from(Employee)
        .outerjoin(current, true())
        .outerjoin(reference, true())
        .where(Employee.tenant_id == tenant_id, Employee.id.between(first_id, last_id))
    )


async def _balances(
    session: AsyncSession, tenant_id: UUID, employee_ids: list[UUID], year: int, *, lock: bool
) -> dict[tuple[UUID, UUID], LeaveBalance]:
    if not employee_ids:
        return {}
    stmt = select(LeaveBalance).where(
        LeaveBalance.tenant_id == tenant_id,
        LeaveBalance.employee_id.in_(employee_ids),
        LeaveBalance.year == year,
    )
    if lock:
        stmt = stmt.with_for_update()
    return {(b.employee_id, b.leave_type_id): b for b in await session.scalars(stmt)}


def _snapshot(balance: LeaveBalance) -> dict[str, Any]:
    return {
        "entitled": balance.entitled,
        "carried_over": balance.carried_over,
        "expired": balance.expired,
        "carry_over_expires_on": (
            balance.carry_over_expires_on.isoformat() if balance.carry_over_expires_on else None
        ),
    }


async def accrue(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    first_id: UUID,
    last_id: UUID,
    as_of: date,
    actor_user_id: UUID | None,
    job_run_id: UUID,
) -> ChunkResult:
    result = ChunkResult()
    year = as_of.year
    leave_types = list(
        await session.scalars(
            select(LeaveType)
            .where(
                LeaveType.tenant_id == tenant_id,
                LeaveType.is_active.is_(True),
                LeaveType.requires_balance.is_(True),
            )
            .order_by(LeaveType.code)
        )
    )
    if not leave_types:
        return result
    policies = await load_policies(session, tenant_id)
    rows = [
        row
        for row in (
            await session.execute(_chunk_employees(tenant_id, first_id, last_id, as_of))
        ).all()
        if row.cur_status == EmploymentStatus.ACTIVE
    ]
    ids = [row.id for row in rows]
    current = await _balances(session, tenant_id, ids, year, lock=True)
    previous = await _balances(session, tenant_id, ids, year - 1, lock=False)
    now = datetime.now(UTC)
    changed: list[tuple[LeaveBalance, LeaveType, dict[str, Any] | None]] = []

    for row in rows:
        reference_date = max(date(year, 1, 1), row.hire_date)
        for leave_type in leave_types:
            base = rules.pick_policy(
                policies,
                leave_type_id=leave_type.id,
                grade=row.ref_grade,
                months=rules.service_months(row.hire_date, reference_date),
            )
            target = rules.pick_policy(
                policies,
                leave_type_id=leave_type.id,
                grade=row.cur_grade,
                months=rules.service_months(row.hire_date, as_of),
            )
            detail = {"employee_id": str(row.id), "leave_type": leave_type.code}
            balance = current.get((row.id, leave_type.id))
            before = None if balance is None else _snapshot(balance)
            if balance is None:
                balance = LeaveBalance(
                    tenant_id=tenant_id,
                    employee_id=row.id,
                    leave_type_id=leave_type.id,
                    year=year,
                    entitled=base.annual_days if base else 0,
                    carried_over=0,
                    adjusted=0,
                    used=0,
                    pending=0,
                    expired=0,
                )
                session.add(balance)
                result.add("balance_created", **detail, entitled=balance.entitled)

            target_days = target.annual_days if target else 0
            if target_days > balance.entitled:
                result.add(
                    "entitlement_increased", **detail, before=balance.entitled, after=target_days
                )
                balance.entitled = target_days

            if balance.carry_over_applied_at is None:
                last_year = previous.get((row.id, leave_type.id))
                carry = 0
                if last_year is not None and base is not None:
                    carry = min(max(last_year.available, 0), base.max_carry_over_days)
                if carry > 0:
                    balance.carried_over += carry
                    balance.carry_over_expires_on = rules.carry_over_expiry(
                        year, base.carry_over_expiry_months if base else 0
                    )
                    result.add(
                        "carried_over",
                        **detail,
                        days=carry,
                        expires_on=(
                            balance.carry_over_expires_on.isoformat()
                            if balance.carry_over_expires_on
                            else None
                        ),
                    )
                balance.carry_over_applied_at = now

            if before is None or before != _snapshot(balance):
                changed.append((balance, leave_type, before))

    await session.flush()
    for balance, leave_type, before in changed:
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action="leave_balance.accrual",
            entity_type="leave_balance",
            entity_id=balance.id,
            before=before,
            after=_snapshot(balance)
            | {"leave_type": leave_type.code, "year": year, "job_run_id": str(job_run_id)},
        )
    return result


async def expire_carry_over(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    first_id: UUID,
    last_id: UUID,
    as_of: date,
    actor_user_id: UUID | None,
    job_run_id: UUID,
) -> ChunkResult:
    """Sisa carry-over yang belum dipakai (termasuk yang sedang diajukan) hangus."""
    result = ChunkResult()
    balances = await session.scalars(
        select(LeaveBalance)
        .join(
            LeaveType,
            and_(
                LeaveType.tenant_id == LeaveBalance.tenant_id,
                LeaveType.id == LeaveBalance.leave_type_id,
            ),
        )
        .where(
            LeaveBalance.tenant_id == tenant_id,
            LeaveBalance.employee_id.between(first_id, last_id),
            LeaveBalance.year == as_of.year,
            LeaveBalance.carry_over_expires_on <= as_of,
            LeaveBalance.carry_over_expired_at.is_(None),
        )
        .with_for_update(of=LeaveBalance)
    )
    now = datetime.now(UTC)
    for balance in balances.all():
        before = _snapshot(balance)
        unused = max(0, balance.carried_over - balance.used - balance.pending)
        balance.expired += unused
        balance.carry_over_expired_at = now
        detail = {"employee_id": str(balance.employee_id), "days": unused}
        result.add("carry_over_expired" if unused else "carry_over_fully_used", **detail)
        if unused:
            await record_audit(
                session,
                tenant_id=tenant_id,
                actor_user_id=actor_user_id,
                action="leave_balance.carry_over_expired",
                entity_type="leave_balance",
                entity_id=balance.id,
                before=before,
                after=_snapshot(balance) | {"job_run_id": str(job_run_id)},
            )
    await session.flush()
    return result
