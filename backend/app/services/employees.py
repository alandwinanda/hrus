"""Data karyawan dan riwayat jabatan effective-dated.

Jabatan yang berlaku per tanggal X diambil lewat LATERAL join: baris employee_job dengan
effdt terbesar yang <= X, lalu effseq terbesar. Index (tenant_id, employee_id, effdt, effseq)
membuat pencarian ini satu index lookup per karyawan.
"""

from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy import Select, exists, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import paginate
from app.core.security import AccessClaims
from app.models import Employee, EmployeeJob, EmploymentStatus, JobAction, Role
from app.rules import core_hr as rules
from app.schemas.common import Page
from app.schemas.employee import (
    CurrentJob,
    EmployeeCreate,
    EmployeeJobCreate,
    EmployeeJobRead,
    EmployeeRead,
    EmployeeStatusFilter,
    EmployeeUpdate,
)
from app.services.audit import record_audit
from app.services.org_units import ensure_active_org_unit, ensure_employee_exists
from app.services.tenant import tenant_today

_JOB_FIELDS = (
    "effdt",
    "effseq",
    "action",
    "job_title",
    "grade",
    "org_unit_id",
    "supervisor_employee_id",
    "employment_type",
    "employment_status",
)


def _current_job_select(as_of: date) -> Select[Any]:
    """Karyawan + jabatan yang berlaku per as_of (kosong kalau belum mulai bekerja)."""
    current = (
        select(*(getattr(EmployeeJob, name).label(f"job_{name}") for name in _JOB_FIELDS))
        .where(
            EmployeeJob.tenant_id == Employee.tenant_id,
            EmployeeJob.employee_id == Employee.id,
            EmployeeJob.effdt <= as_of,
        )
        .order_by(EmployeeJob.effdt.desc(), EmployeeJob.effseq.desc())
        .limit(1)
        .lateral("current_job")
    )
    return (
        select(
            Employee.id,
            Employee.employee_number,
            Employee.full_name,
            Employee.work_email,
            Employee.hire_date,
            *current.c,
        )
        .select_from(Employee)
        .outerjoin(current, true())
    )


def _to_read(row: Any) -> EmployeeRead:
    data = dict(row)
    job = {name: data.pop(f"job_{name}") for name in _JOB_FIELDS}
    current_job = CurrentJob.model_validate(job) if job["effdt"] is not None else None
    status = current_job.employment_status.value if current_job else "pre_hire"
    return EmployeeRead.model_validate({**data, "status": status, "current_job": current_job})


async def list_employees(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    cursor: str | None,
    limit: int,
    status: EmployeeStatusFilter = "active",
    org_unit_id: UUID | None = None,
    supervisor_id: UUID | None = None,
    as_of: date | None = None,
) -> Page[EmployeeRead]:
    as_of = as_of or await tenant_today(session, tenant_id)
    stmt = _current_job_select(as_of).where(Employee.tenant_id == tenant_id)
    job = stmt.selected_columns
    if status == "pre_hire":
        stmt = stmt.where(job.job_effdt.is_(None))
    elif status != "all":
        stmt = stmt.where(job.job_employment_status == status)
    if org_unit_id is not None:
        stmt = stmt.where(job.job_org_unit_id == org_unit_id)
    if supervisor_id is not None:
        stmt = stmt.where(job.job_supervisor_employee_id == supervisor_id)

    page = await paginate(session, stmt, [Employee.employee_number, Employee.id], cursor, limit)
    return Page(items=[_to_read(row) for row in page.items], next_cursor=page.next_cursor)


async def _read(
    session: AsyncSession, tenant_id: UUID, employee_id: UUID, as_of: date
) -> EmployeeRead:
    row = (
        (
            await session.execute(
                _current_job_select(as_of).where(
                    Employee.tenant_id == tenant_id, Employee.id == employee_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise NotFoundError("Karyawan tidak ditemukan.")
    return _to_read(row)


def _can_view(viewer: AccessClaims, employee: EmployeeRead) -> bool:
    if Role.HR_ADMIN in viewer.roles or viewer.employee_id == employee.id:
        return True
    job = employee.current_job
    return (
        Role.MANAGER in viewer.roles
        and viewer.employee_id is not None
        and job is not None
        and job.supervisor_employee_id == viewer.employee_id
    )


async def get_employee(
    session: AsyncSession, viewer: AccessClaims, employee_id: UUID, *, as_of: date | None = None
) -> EmployeeRead:
    """HR melihat semua, karyawan melihat dirinya, atasan melihat bawahan langsungnya.

    Selain itu 404 (bukan 403), supaya keberadaan data karyawan lain tidak bocor.
    """
    as_of = as_of or await tenant_today(session, viewer.tenant_id)
    employee = await _read(session, viewer.tenant_id, employee_id, as_of)
    if not _can_view(viewer, employee):
        raise NotFoundError("Karyawan tidak ditemukan.")
    return employee


async def _ensure_unique(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    employee_number: str | None = None,
    work_email: str | None = None,
    exclude_id: UUID | None = None,
) -> None:
    checks = [
        (employee_number, Employee.employee_number, "employee_number_taken", "Nomor karyawan"),
        (work_email, Employee.work_email, "work_email_taken", "Email kantor"),
    ]
    for value, column, code, label in checks:
        if value is None:
            continue
        condition = [Employee.tenant_id == tenant_id, column == value]
        if exclude_id is not None:
            condition.append(Employee.id != exclude_id)
        if await session.scalar(select(exists().where(*condition))):
            raise ConflictError(f"{label} '{value}' sudah dipakai.", code=code)


async def create_employee(
    session: AsyncSession, actor: AccessClaims, data: EmployeeCreate
) -> EmployeeRead:
    tenant_id = actor.tenant_id
    employee_number = data.employee_number.strip().upper()
    await _ensure_unique(
        session, tenant_id, employee_number=employee_number, work_email=data.work_email
    )
    await ensure_active_org_unit(session, tenant_id, data.job.org_unit_id)
    if data.job.supervisor_employee_id is not None:
        await ensure_employee_exists(
            session, tenant_id, data.job.supervisor_employee_id, field="supervisor_employee_id"
        )

    employee = Employee(
        tenant_id=tenant_id,
        employee_number=employee_number,
        full_name=data.full_name.strip(),
        work_email=data.work_email,
        hire_date=data.hire_date,
    )
    session.add(employee)
    await session.flush()
    job = EmployeeJob(
        tenant_id=tenant_id,
        employee_id=employee.id,
        effdt=data.hire_date,
        effseq=0,
        action=JobAction.HIRE,
        job_title=data.job.job_title.strip(),
        grade=data.job.grade.strip(),
        org_unit_id=data.job.org_unit_id,
        supervisor_employee_id=data.job.supervisor_employee_id,
        employment_type=data.job.employment_type,
        employment_status=EmploymentStatus.ACTIVE,
    )
    session.add(job)
    await session.flush()

    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="employee.create",
        entity_type="employee",
        entity_id=employee.id,
        after={
            "employee_number": employee.employee_number,
            "full_name": employee.full_name,
            "work_email": employee.work_email,
            "hire_date": employee.hire_date.isoformat(),
            "job": EmployeeJobRead.model_validate(job).model_dump(mode="json"),
        },
    )
    return await _read(session, tenant_id, employee.id, await tenant_today(session, tenant_id))


async def update_employee(
    session: AsyncSession, actor: AccessClaims, employee_id: UUID, data: EmployeeUpdate
) -> EmployeeRead:
    tenant_id = actor.tenant_id
    employee = await session.scalar(
        select(Employee).where(Employee.tenant_id == tenant_id, Employee.id == employee_id)
    )
    if employee is None:
        raise NotFoundError("Karyawan tidak ditemukan.")

    before = {"full_name": employee.full_name, "work_email": employee.work_email}
    fields = data.model_fields_set
    if "work_email" in fields:
        await _ensure_unique(session, tenant_id, work_email=data.work_email, exclude_id=employee.id)
        employee.work_email = data.work_email
    if "full_name" in fields and data.full_name is not None:
        employee.full_name = data.full_name.strip()
    after = {"full_name": employee.full_name, "work_email": employee.work_email}

    if after != before:
        await session.flush()
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor.user_id,
            action="employee.update",
            entity_type="employee",
            entity_id=employee.id,
            before=before,
            after=after,
        )
    return await _read(session, tenant_id, employee.id, await tenant_today(session, tenant_id))


async def list_jobs(
    session: AsyncSession,
    viewer: AccessClaims,
    employee_id: UUID,
    *,
    cursor: str | None,
    limit: int,
) -> Page[EmployeeJobRead]:
    """Riwayat jabatan terbaru dulu. HR atau karyawan itu sendiri."""
    tenant_id = viewer.tenant_id
    allowed = Role.HR_ADMIN in viewer.roles or viewer.employee_id == employee_id
    found = await session.scalar(
        select(exists().where(Employee.tenant_id == tenant_id, Employee.id == employee_id))
    )
    if not (allowed and found):
        raise NotFoundError("Karyawan tidak ditemukan.")

    stmt = select(EmployeeJob).where(
        EmployeeJob.tenant_id == tenant_id, EmployeeJob.employee_id == employee_id
    )
    page = await paginate(
        session,
        stmt,
        [EmployeeJob.effdt, EmployeeJob.effseq, EmployeeJob.id],
        cursor,
        limit,
        descending=True,
    )
    return Page(
        items=[EmployeeJobRead.model_validate(job) for job in page.items],
        next_cursor=page.next_cursor,
    )


async def add_job(
    session: AsyncSession, actor: AccessClaims, employee_id: UUID, data: EmployeeJobCreate
) -> EmployeeJobRead:
    """Tambah baris riwayat jabatan. Riwayat lama tidak pernah diubah."""
    tenant_id = actor.tenant_id
    # FOR UPDATE: dua perubahan bersamaan untuk karyawan yang sama diproses bergantian,
    # jadi effseq tidak bentrok.
    employee = await session.scalar(
        select(Employee)
        .where(Employee.tenant_id == tenant_id, Employee.id == employee_id)
        .with_for_update()
    )
    if employee is None:
        raise NotFoundError("Karyawan tidak ditemukan.")

    history = EmployeeJob.tenant_id == tenant_id, EmployeeJob.employee_id == employee.id
    latest_effdt = await session.scalar(select(func.max(EmployeeJob.effdt)).where(*history))
    rules.check_new_job_date(
        action=data.action,
        effdt=data.effdt,
        hire_date=employee.hire_date,
        latest_effdt=latest_effdt,
    )
    previous = await session.scalar(
        select(EmployeeJob)
        .where(*history, EmployeeJob.effdt <= data.effdt)
        .order_by(EmployeeJob.effdt.desc(), EmployeeJob.effseq.desc())
        .limit(1)
    )
    if previous is None:  # pragma: no cover - selalu ada baris hire per hire_date
        raise NotFoundError("Riwayat jabatan awal tidak ditemukan.")
    rules.check_action_transition(
        action=data.action, status_before=EmploymentStatus(previous.employment_status)
    )

    fields = data.model_fields_set
    org_unit_id = data.org_unit_id or previous.org_unit_id
    supervisor_id = (
        data.supervisor_employee_id
        if "supervisor_employee_id" in fields
        else previous.supervisor_employee_id
    )
    if data.org_unit_id is not None:
        await ensure_active_org_unit(session, tenant_id, org_unit_id)
    rules.check_supervisor(employee_id=employee.id, supervisor_id=supervisor_id)
    if supervisor_id is not None and supervisor_id != previous.supervisor_employee_id:
        await ensure_employee_exists(
            session, tenant_id, supervisor_id, field="supervisor_employee_id"
        )

    last_seq = await session.scalar(
        select(func.max(EmployeeJob.effseq)).where(*history, EmployeeJob.effdt == data.effdt)
    )
    job = EmployeeJob(
        tenant_id=tenant_id,
        employee_id=employee.id,
        effdt=data.effdt,
        effseq=0 if last_seq is None else last_seq + 1,
        action=data.action,
        reason=data.reason.strip() if data.reason else None,
        job_title=(data.job_title or previous.job_title).strip(),
        grade=(data.grade or previous.grade).strip(),
        org_unit_id=org_unit_id,
        supervisor_employee_id=supervisor_id,
        employment_type=data.employment_type or previous.employment_type,
        employment_status=rules.employment_status_for(data.action),
    )
    session.add(job)
    await session.flush()
    await session.refresh(job, ["created_at"])
    result = EmployeeJobRead.model_validate(job)
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="employee_job.create",
        entity_type="employee",
        entity_id=employee.id,
        after=result.model_dump(mode="json", exclude={"created_at"}),
    )
    return result
