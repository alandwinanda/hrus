"""Konfigurasi cuti oleh HR: tipe cuti, policy jatah, dan hari libur."""

from datetime import date
from uuid import UUID

from sqlalchemy import delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.pagination import paginate
from app.core.security import AccessClaims
from app.models import HolidayCalendar, LeavePolicy, LeaveType
from app.schemas.common import Page
from app.schemas.leave import (
    HolidayCreate,
    HolidayRead,
    LeavePolicyCreate,
    LeavePolicyRead,
    LeavePolicyUpdate,
    LeaveTypeCreate,
    LeaveTypeRead,
    LeaveTypeUpdate,
)
from app.services.audit import record_audit

# --- Tipe cuti --------------------------------------------------------------------------


async def list_leave_types(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    cursor: str | None,
    limit: int,
    include_inactive: bool = False,
) -> Page[LeaveTypeRead]:
    stmt = select(LeaveType).where(LeaveType.tenant_id == tenant_id)
    if not include_inactive:
        stmt = stmt.where(LeaveType.is_active.is_(True))
    page = await paginate(session, stmt, [LeaveType.code, LeaveType.id], cursor, limit)
    return Page(
        items=[LeaveTypeRead.model_validate(t) for t in page.items], next_cursor=page.next_cursor
    )


async def get_leave_type(session: AsyncSession, tenant_id: UUID, type_id: UUID) -> LeaveType:
    leave_type = await session.scalar(
        select(LeaveType).where(LeaveType.tenant_id == tenant_id, LeaveType.id == type_id)
    )
    if leave_type is None:
        raise NotFoundError("Tipe cuti tidak ditemukan.")
    return leave_type


async def create_leave_type(
    session: AsyncSession, actor: AccessClaims, data: LeaveTypeCreate
) -> LeaveTypeRead:
    code = data.code.strip().upper()
    taken = await session.scalar(
        select(exists().where(LeaveType.tenant_id == actor.tenant_id, LeaveType.code == code))
    )
    if taken:
        raise ConflictError(f"Kode tipe cuti '{code}' sudah dipakai.", code="leave_type_code_taken")
    leave_type = LeaveType(tenant_id=actor.tenant_id, **data.model_dump() | {"code": code})
    leave_type.is_active = True
    session.add(leave_type)
    await session.flush()
    result = LeaveTypeRead.model_validate(leave_type)
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="leave_type.create",
        entity_type="leave_type",
        entity_id=leave_type.id,
        after=result.model_dump(mode="json"),
    )
    return result


async def update_leave_type(
    session: AsyncSession, actor: AccessClaims, type_id: UUID, data: LeaveTypeUpdate
) -> LeaveTypeRead:
    leave_type = await get_leave_type(session, actor.tenant_id, type_id)
    before = LeaveTypeRead.model_validate(leave_type)
    for field in data.model_fields_set:
        value = getattr(data, field)
        # max_days_per_request boleh dikosongkan (null = tanpa batas), field lain tidak.
        if value is not None or field == "max_days_per_request":
            setattr(leave_type, field, value)
    await session.flush()
    after = LeaveTypeRead.model_validate(leave_type)
    if after != before:
        await record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            action="leave_type.update",
            entity_type="leave_type",
            entity_id=leave_type.id,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
        )
    return after


# --- Policy -----------------------------------------------------------------------------


async def list_policies(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    leave_type_id: UUID | None,
    cursor: str | None,
    limit: int,
) -> Page[LeavePolicyRead]:
    stmt = select(LeavePolicy).where(LeavePolicy.tenant_id == tenant_id)
    if leave_type_id is not None:
        stmt = stmt.where(LeavePolicy.leave_type_id == leave_type_id)
    page = await paginate(
        session,
        stmt,
        [LeavePolicy.leave_type_id, LeavePolicy.min_service_months, LeavePolicy.id],
        cursor,
        limit,
    )
    return Page(
        items=[LeavePolicyRead.model_validate(p) for p in page.items],
        next_cursor=page.next_cursor,
    )


async def _get_policy(session: AsyncSession, tenant_id: UUID, policy_id: UUID) -> LeavePolicy:
    policy = await session.scalar(
        select(LeavePolicy).where(LeavePolicy.tenant_id == tenant_id, LeavePolicy.id == policy_id)
    )
    if policy is None:
        raise NotFoundError("Policy cuti tidak ditemukan.")
    return policy


async def create_policy(
    session: AsyncSession, actor: AccessClaims, data: LeavePolicyCreate
) -> LeavePolicyRead:
    await get_leave_type(session, actor.tenant_id, data.leave_type_id)
    grade = data.grade.strip().upper() if data.grade else None
    duplicate = await session.scalar(
        select(
            exists().where(
                LeavePolicy.tenant_id == actor.tenant_id,
                LeavePolicy.leave_type_id == data.leave_type_id,
                func.coalesce(LeavePolicy.grade, "") == (grade or ""),
                LeavePolicy.min_service_months == data.min_service_months,
            )
        )
    )
    if duplicate:
        raise ConflictError(
            "Policy untuk tipe cuti, grade, dan masa kerja ini sudah ada.",
            code="leave_policy_exists",
        )
    policy = LeavePolicy(tenant_id=actor.tenant_id, **data.model_dump() | {"grade": grade})
    session.add(policy)
    await session.flush()
    result = LeavePolicyRead.model_validate(policy)
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="leave_policy.create",
        entity_type="leave_policy",
        entity_id=policy.id,
        after=result.model_dump(mode="json"),
    )
    return result


async def update_policy(
    session: AsyncSession, actor: AccessClaims, policy_id: UUID, data: LeavePolicyUpdate
) -> LeavePolicyRead:
    """Berlaku untuk saldo yang dibuat setelah ini. Saldo yang sudah ada tidak berubah."""
    policy = await _get_policy(session, actor.tenant_id, policy_id)
    before = LeavePolicyRead.model_validate(policy)
    for field in data.model_fields_set:
        value = getattr(data, field)
        if value is not None:
            setattr(policy, field, value)
    await session.flush()
    after = LeavePolicyRead.model_validate(policy)
    if after != before:
        await record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            action="leave_policy.update",
            entity_type="leave_policy",
            entity_id=policy.id,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
        )
    return after


async def delete_policy(session: AsyncSession, actor: AccessClaims, policy_id: UUID) -> None:
    policy = await _get_policy(session, actor.tenant_id, policy_id)
    before = LeavePolicyRead.model_validate(policy).model_dump(mode="json")
    await session.execute(
        delete(LeavePolicy).where(
            LeavePolicy.tenant_id == actor.tenant_id, LeavePolicy.id == policy.id
        )
    )
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="leave_policy.delete",
        entity_type="leave_policy",
        entity_id=policy_id,
        before=before,
    )


# --- Hari libur -------------------------------------------------------------------------


async def list_holidays(
    session: AsyncSession, tenant_id: UUID, *, year: int, cursor: str | None, limit: int
) -> Page[HolidayRead]:
    stmt = select(HolidayCalendar).where(
        HolidayCalendar.tenant_id == tenant_id,
        HolidayCalendar.holiday_date.between(date(year, 1, 1), date(year, 12, 31)),
    )
    page = await paginate(
        session, stmt, [HolidayCalendar.holiday_date, HolidayCalendar.id], cursor, limit
    )
    return Page(
        items=[HolidayRead.model_validate(h) for h in page.items], next_cursor=page.next_cursor
    )


async def holidays_between(
    session: AsyncSession, tenant_id: UUID, start: date, end: date
) -> set[date]:
    rows = await session.scalars(
        select(HolidayCalendar.holiday_date).where(
            HolidayCalendar.tenant_id == tenant_id,
            HolidayCalendar.holiday_date.between(start, end),
        )
    )
    return set(rows)


async def create_holiday(
    session: AsyncSession, actor: AccessClaims, data: HolidayCreate
) -> HolidayRead:
    taken = await session.scalar(
        select(
            exists().where(
                HolidayCalendar.tenant_id == actor.tenant_id,
                HolidayCalendar.holiday_date == data.holiday_date,
            )
        )
    )
    if taken:
        raise ConflictError("Sudah ada hari libur di tanggal ini.", code="holiday_exists")
    holiday = HolidayCalendar(tenant_id=actor.tenant_id, **data.model_dump())
    session.add(holiday)
    await session.flush()
    result = HolidayRead.model_validate(holiday)
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="holiday.create",
        entity_type="holiday_calendar",
        entity_id=holiday.id,
        after=result.model_dump(mode="json"),
    )
    return result


async def delete_holiday(session: AsyncSession, actor: AccessClaims, holiday_id: UUID) -> None:
    """Pengajuan cuti yang sudah dibuat tidak dihitung ulang."""
    holiday = await session.scalar(
        select(HolidayCalendar).where(
            HolidayCalendar.tenant_id == actor.tenant_id, HolidayCalendar.id == holiday_id
        )
    )
    if holiday is None:
        raise NotFoundError("Hari libur tidak ditemukan.")
    before = HolidayRead.model_validate(holiday).model_dump(mode="json")
    await session.execute(
        delete(HolidayCalendar).where(
            HolidayCalendar.tenant_id == actor.tenant_id, HolidayCalendar.id == holiday.id
        )
    )
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="holiday.delete",
        entity_type="holiday_calendar",
        entity_id=holiday_id,
        before=before,
    )
