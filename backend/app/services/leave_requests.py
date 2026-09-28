"""Pengajuan cuti: validate (dry-run), submit, approval berjenjang, cancel, daftar, dan kalender.

Endpoint validate dan submit memakai evaluasi yang sama, jadi hasil validasi di form dan chat
selalu konsisten. Jumlah hari dan saldo selalu dihitung di sini, tidak pernah oleh LLM.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Select, and_, exists, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, ForbiddenError, NotFoundError, RuleViolationError
from app.core.pagination import paginate
from app.core.security import AccessClaims
from app.models import (
    ApprovalStatus,
    Employee,
    EmployeeJob,
    EmploymentStatus,
    LeaveApproval,
    LeaveBalance,
    LeaveRequest,
    LeaveRequestStatus,
    LeaveType,
    Role,
)
from app.models.leave import MAX_REQUEST_SPAN_DAYS
from app.rules import leave as rules
from app.schemas.common import Page
from app.schemas.leave import (
    BalancePreview,
    LeaveApprovalRead,
    LeaveCancel,
    LeaveDecision,
    LeaveRequestDetail,
    LeaveRequestInput,
    LeaveRequestRead,
    LeaveRequestScope,
    LeaveValidationResult,
    RuleMessage,
    TeamCalendarItem,
    TeamOverlapWarning,
)
from app.services import employees
from app.services.audit import record_audit
from app.services.leave_balances import get_balance
from app.services.leave_config import get_leave_type, holidays_between
from app.services.tenant import tenant_today

MAX_CALENDAR_DAYS = 93

# Ditulis literal (bukan bind parameter) supaya planner bisa memakai partial index
# ex_leave_request_no_overlap.
_ACTIVE = text("leave_request.status IN ('pending', 'approved')")


def _overlaps(start: date, end: date) -> ColumnElement[bool]:
    """Pengajuan yang beririsan dengan [start, end].

    Perbandingan tanggal biasa (leakproof) supaya index btree tetap terpakai di bawah RLS.
    Batas bawah start_date aman karena rentang satu pengajuan dibatasi check constraint.
    """
    return and_(
        LeaveRequest.start_date <= end,
        LeaveRequest.end_date >= start,
        LeaveRequest.start_date > start - timedelta(days=MAX_REQUEST_SPAN_DAYS),
    )


def _no_profile() -> RuleViolationError:
    return RuleViolationError(
        "Akun ini belum terhubung dengan data karyawan.", code="no_employee_profile"
    )


# --- Evaluasi (dipakai validate dan submit) ---------------------------------------------


@dataclass(slots=True)
class _Evaluation:
    result: LeaveValidationResult
    leave_type: LeaveType
    balance: LeaveBalance | None


async def _team_overlap(
    session: AsyncSession,
    tenant_id: UUID,
    employee_id: UUID,
    job: EmployeeJob | None,
    start: date,
    end: date,
) -> list[TeamOverlapWarning]:
    """Soft warning deterministik: rekan satu atasan yang juga cuti di tanggal yang sama."""
    if job is None or job.supervisor_employee_id is None:
        return []
    team = employees.employees_as_of_query(
        tenant_id, start, supervisor_id=job.supervisor_employee_id
    )
    count = await session.scalar(
        select(func.count(func.distinct(LeaveRequest.employee_id))).where(
            LeaveRequest.tenant_id == tenant_id,
            LeaveRequest.employee_id.in_(team),
            LeaveRequest.employee_id != employee_id,
            _ACTIVE,
            _overlaps(start, end),
        )
    )
    if not count:
        return []
    return [
        TeamOverlapWarning(
            code="team_overlap",
            message=f"{count} rekan satu tim juga cuti atau mengajukan cuti di tanggal ini.",
            colleagues_on_leave=count,
        )
    ]


async def _evaluate(
    session: AsyncSession, actor: AccessClaims, data: LeaveRequestInput, *, lock: bool
) -> _Evaluation:
    if actor.employee_id is None:
        raise _no_profile()
    tenant_id, employee_id = actor.tenant_id, actor.employee_id
    leave_type = await get_leave_type(session, tenant_id, data.leave_type_id)
    errors: list[rules.Violation] = []

    def add(violation: rules.Violation | None) -> None:
        if violation is not None:
            errors.append(violation)

    if not leave_type.is_active:
        add(rules.Violation("leave_type_inactive", "Tipe cuti ini sudah tidak aktif."))
    range_error = rules.check_range(data.start_date, data.end_date)
    if range_error is not None:
        errors.append(range_error)
        return _Evaluation(_result(0, errors, [], None), leave_type, None)

    today = await tenant_today(session, tenant_id)
    holidays = await holidays_between(session, tenant_id, data.start_date, data.end_date)
    days = rules.working_days(data.start_date, data.end_date, holidays)
    job = await employees.job_as_of(session, tenant_id, employee_id, data.start_date)

    add(rules.check_working_days(days))
    add(rules.check_employee_active(EmploymentStatus(job.employment_status) if job else None))
    add(
        rules.check_notice(
            start=data.start_date,
            today=today,
            min_notice_days=leave_type.min_notice_days,
            allow_backdated=leave_type.allow_backdated,
        )
    )
    add(rules.check_max_days(days, leave_type.max_days_per_request))
    overlap = await session.scalar(
        select(
            exists().where(
                LeaveRequest.tenant_id == tenant_id,
                LeaveRequest.employee_id == employee_id,
                _ACTIVE,
                _overlaps(data.start_date, data.end_date),
            )
        )
    )
    add(rules.check_no_overlap(bool(overlap)))

    balance = None
    preview = None
    if leave_type.requires_balance:
        balance = await get_balance(
            session, tenant_id, employee_id, leave_type, data.start_date.year, lock=lock
        )
        add(rules.check_balance(available=balance.available, days=days))
        preview = BalancePreview(
            available_before=balance.available, available_after=balance.available - days
        )

    warnings = await _team_overlap(
        session, tenant_id, employee_id, job, data.start_date, data.end_date
    )
    return _Evaluation(_result(days, errors, warnings, preview), leave_type, balance)


def _result(
    days: int,
    errors: list[rules.Violation],
    warnings: list[TeamOverlapWarning],
    balance: BalancePreview | None,
) -> LeaveValidationResult:
    return LeaveValidationResult(
        valid=not errors,
        days=days,
        errors=[RuleMessage(code=e.code, message=e.message) for e in errors],
        warnings=warnings,
        balance=balance,
    )


async def validate(
    session: AsyncSession, actor: AccessClaims, data: LeaveRequestInput
) -> LeaveValidationResult:
    """Dry-run: tidak menyimpan pengajuan dan tidak mengubah saldo."""
    return (await _evaluate(session, actor, data, lock=False)).result


# --- Baca -------------------------------------------------------------------------------


def _request_select() -> Select[Any]:
    return (
        select(
            LeaveRequest.id,
            LeaveRequest.employee_id,
            Employee.full_name.label("employee_name"),
            LeaveRequest.leave_type_id,
            LeaveType.code.label("leave_type_code"),
            LeaveRequest.start_date,
            LeaveRequest.end_date,
            LeaveRequest.days,
            LeaveRequest.reason,
            LeaveRequest.status,
            LeaveRequest.approval_levels,
            LeaveRequest.current_level,
            LeaveRequest.created_at,
        )
        .select_from(LeaveRequest)
        .join(
            Employee,
            and_(
                Employee.tenant_id == LeaveRequest.tenant_id,
                Employee.id == LeaveRequest.employee_id,
            ),
        )
        .join(
            LeaveType,
            and_(
                LeaveType.tenant_id == LeaveRequest.tenant_id,
                LeaveType.id == LeaveRequest.leave_type_id,
            ),
        )
    )


async def _can_view(session: AsyncSession, viewer: AccessClaims, request: LeaveRequest) -> bool:
    if Role.HR_ADMIN in viewer.roles or viewer.employee_id == request.employee_id:
        return True
    if viewer.employee_id is None:
        return False
    return bool(
        await session.scalar(
            select(
                exists().where(
                    LeaveApproval.tenant_id == request.tenant_id,
                    LeaveApproval.leave_request_id == request.id,
                    LeaveApproval.approver_employee_id == viewer.employee_id,
                )
            )
        )
    )


async def _load(
    session: AsyncSession, tenant_id: UUID, request_id: UUID, *, lock: bool = False
) -> LeaveRequest:
    stmt = select(LeaveRequest).where(
        LeaveRequest.tenant_id == tenant_id, LeaveRequest.id == request_id
    )
    if lock:
        stmt = stmt.with_for_update()
    request = await session.scalar(stmt)
    if request is None:
        raise NotFoundError("Pengajuan cuti tidak ditemukan.")
    return request


async def _detail(session: AsyncSession, tenant_id: UUID, request_id: UUID) -> LeaveRequestDetail:
    row = (
        (
            await session.execute(
                _request_select().where(
                    LeaveRequest.tenant_id == tenant_id, LeaveRequest.id == request_id
                )
            )
        )
        .mappings()
        .one()
    )
    approvals = await session.scalars(
        select(LeaveApproval)
        .where(LeaveApproval.tenant_id == tenant_id, LeaveApproval.leave_request_id == request_id)
        .order_by(LeaveApproval.level)
    )
    validation = await session.scalar(
        select(LeaveRequest.validation).where(
            LeaveRequest.tenant_id == tenant_id, LeaveRequest.id == request_id
        )
    )
    return LeaveRequestDetail.model_validate(
        dict(row)
        | {
            "approvals": [LeaveApprovalRead.model_validate(a) for a in approvals],
            "warnings": (validation or {}).get("warnings", []),
        }
    )


async def get_request(
    session: AsyncSession, viewer: AccessClaims, request_id: UUID
) -> LeaveRequestDetail:
    """Pemohon, HR, dan approver pengajuan itu. Selain itu 404."""
    request = await _load(session, viewer.tenant_id, request_id)
    if not await _can_view(session, viewer, request):
        raise NotFoundError("Pengajuan cuti tidak ditemukan.")
    return await _detail(session, viewer.tenant_id, request_id)


async def list_requests(
    session: AsyncSession,
    viewer: AccessClaims,
    *,
    scope: LeaveRequestScope,
    status: LeaveRequestStatus | None,
    year: int | None,
    cursor: str | None,
    limit: int,
) -> Page[LeaveRequestRead]:
    """mine: pengajuan sendiri. approvals: menunggu keputusan saya. all: semua (HR).

    Default tahun berjalan (berdasarkan tanggal mulai), kecuali scope approvals yang
    menampilkan semua yang masih menunggu.
    """
    tenant_id = viewer.tenant_id
    is_hr = Role.HR_ADMIN in viewer.roles
    stmt = _request_select().where(LeaveRequest.tenant_id == tenant_id)

    if scope == "approvals":
        stmt = stmt.join(
            LeaveApproval,
            and_(
                LeaveApproval.tenant_id == LeaveRequest.tenant_id,
                LeaveApproval.leave_request_id == LeaveRequest.id,
                LeaveApproval.level == LeaveRequest.current_level,
            ),
        ).where(
            LeaveApproval.status == ApprovalStatus.PENDING,
            LeaveRequest.status == LeaveRequestStatus.PENDING,
        )
        mine = (
            LeaveApproval.approver_employee_id == viewer.employee_id
            if viewer.employee_id is not None
            else None
        )
        # Level tanpa atasan (approver NULL) diputuskan HR.
        hr_queue = LeaveApproval.approver_employee_id.is_(None) if is_hr else None
        conditions = [c for c in (mine, hr_queue) if c is not None]
        if not conditions:
            return Page(items=[], next_cursor=None)
        stmt = stmt.where(or_(*conditions))
    else:
        if scope == "mine":
            if viewer.employee_id is None:
                raise _no_profile()
            stmt = stmt.where(LeaveRequest.employee_id == viewer.employee_id)
        elif not is_hr:
            raise ForbiddenError("Hanya HR yang bisa melihat semua pengajuan.")
        year = year or (await tenant_today(session, tenant_id)).year

    if year is not None:
        stmt = stmt.where(LeaveRequest.start_date.between(date(year, 1, 1), date(year, 12, 31)))
    if status is not None:
        stmt = stmt.where(LeaveRequest.status == status)

    page = await paginate(
        session,
        stmt,
        [LeaveRequest.start_date, LeaveRequest.end_date, LeaveRequest.id],
        cursor,
        limit,
        descending=True,
    )
    return Page(
        items=[LeaveRequestRead.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


async def team_calendar(
    session: AsyncSession,
    viewer: AccessClaims,
    *,
    start: date,
    end: date,
    org_unit_id: UUID | None,
    cursor: str | None,
    limit: int,
) -> Page[TeamCalendarItem]:
    """Cuti yang menunggu atau disetujui di rentang tanggal.

    Atasan: bawahan langsung. HR: semua karyawan, opsional per unit.
    """
    if end < start or (end - start).days + 1 > MAX_CALENDAR_DAYS:
        raise RuleViolationError(
            f"Rentang kalender maksimal {MAX_CALENDAR_DAYS} hari.", code="invalid_calendar_range"
        )
    tenant_id = viewer.tenant_id
    today = await tenant_today(session, tenant_id)
    stmt = (
        _request_select()
        .with_only_columns(
            LeaveRequest.id.label("request_id"),
            LeaveRequest.employee_id,
            Employee.full_name.label("employee_name"),
            LeaveType.code.label("leave_type_code"),
            LeaveRequest.start_date,
            LeaveRequest.end_date,
            LeaveRequest.days,
            LeaveRequest.status,
        )
        .where(LeaveRequest.tenant_id == tenant_id, _ACTIVE, _overlaps(start, end))
    )
    if Role.HR_ADMIN in viewer.roles:
        if org_unit_id is not None:
            unit = employees.employees_as_of_query(tenant_id, today, org_unit_id=org_unit_id)
            stmt = stmt.where(LeaveRequest.employee_id.in_(unit))
    else:
        if viewer.employee_id is None:
            raise _no_profile()
        team = employees.employees_as_of_query(tenant_id, today, supervisor_id=viewer.employee_id)
        stmt = stmt.where(LeaveRequest.employee_id.in_(team))

    page = await paginate(
        session,
        stmt,
        [LeaveRequest.start_date, LeaveRequest.end_date, LeaveRequest.id],
        cursor,
        limit,
    )
    return Page(
        items=[TeamCalendarItem.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


# --- Tulis ------------------------------------------------------------------------------


async def _approver_chain(
    session: AsyncSession, tenant_id: UUID, employee_id: UUID, levels: int, as_of: date
) -> list[UUID | None]:
    """Level 1 = atasan langsung, level 2 = atasannya. None = tidak ada, diputuskan HR."""
    chain: list[UUID | None] = []
    current: UUID | None = employee_id
    for _ in range(levels):
        job = (
            await employees.job_as_of(session, tenant_id, current, as_of)
            if current is not None
            else None
        )
        current = job.supervisor_employee_id if job else None
        chain.append(current)
    return chain


async def submit(
    session: AsyncSession,
    actor: AccessClaims,
    data: LeaveRequestInput,
    *,
    idempotency_key: str | None,
) -> LeaveRequestDetail:
    """Ajukan cuti. Idempotency-Key yang sama mengembalikan pengajuan yang sudah ada."""
    if actor.employee_id is None:
        raise _no_profile()
    tenant_id = actor.tenant_id
    if idempotency_key:
        existing = await session.scalar(
            select(LeaveRequest.id).where(
                LeaveRequest.tenant_id == tenant_id,
                LeaveRequest.employee_id == actor.employee_id,
                LeaveRequest.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return await _detail(session, tenant_id, existing)

    evaluation = await _evaluate(session, actor, data, lock=True)
    result = evaluation.result
    if not result.valid:
        first = result.errors[0]
        raise RuleViolationError(first.message, code=first.code)

    leave_type = evaluation.leave_type
    today = await tenant_today(session, tenant_id)
    approvers = await _approver_chain(
        session, tenant_id, actor.employee_id, leave_type.approval_levels, today
    )
    request = LeaveRequest(
        tenant_id=tenant_id,
        employee_id=actor.employee_id,
        leave_type_id=leave_type.id,
        start_date=data.start_date,
        end_date=data.end_date,
        days=result.days,
        reason=data.reason.strip() if data.reason else None,
        status=LeaveRequestStatus.PENDING,
        approval_levels=leave_type.approval_levels,
        current_level=1,
        requested_by_user_id=actor.user_id,
        idempotency_key=idempotency_key,
        validation={"warnings": [w.model_dump() for w in result.warnings]},
    )
    session.add(request)
    try:
        await session.flush()
    except IntegrityError as exc:
        # Race: dua pengajuan bersamaan lolos validasi, database menolak yang kedua.
        raise ConflictError(
            "Sudah ada pengajuan cuti lain di tanggal yang sama.", code="overlapping_request"
        ) from exc

    if evaluation.balance is not None:
        evaluation.balance.pending += result.days
    session.add_all(
        LeaveApproval(
            tenant_id=tenant_id,
            leave_request_id=request.id,
            level=level,
            approver_employee_id=approver,
            status=ApprovalStatus.PENDING,
        )
        for level, approver in enumerate(approvers, start=1)
    )
    await session.flush()
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="leave_request.submit",
        entity_type="leave_request",
        entity_id=request.id,
        after={
            "leave_type": leave_type.code,
            "start_date": data.start_date.isoformat(),
            "end_date": data.end_date.isoformat(),
            "days": result.days,
            "approvers": [str(a) if a else None for a in approvers],
        },
    )
    return await _detail(session, tenant_id, request.id)


async def _release_balance(
    session: AsyncSession, request: LeaveRequest, *, pending: int = 0, used: int = 0
) -> None:
    leave_type = await get_leave_type(session, request.tenant_id, request.leave_type_id)
    if not leave_type.requires_balance:
        return
    balance = await get_balance(
        session,
        request.tenant_id,
        request.employee_id,
        leave_type,
        request.start_date.year,
        lock=True,
    )
    balance.pending += pending
    balance.used += used


async def _skip_remaining(session: AsyncSession, request: LeaveRequest) -> None:
    await session.execute(
        update(LeaveApproval)
        .where(
            LeaveApproval.tenant_id == request.tenant_id,
            LeaveApproval.leave_request_id == request.id,
            LeaveApproval.status == ApprovalStatus.PENDING,
        )
        .values(status=ApprovalStatus.SKIPPED)
    )


async def decide(
    session: AsyncSession, actor: AccessClaims, request_id: UUID, data: LeaveDecision
) -> LeaveRequestDetail:
    """Keputusan approver di level yang sedang berjalan. HR bisa memutuskan level mana pun."""
    tenant_id = actor.tenant_id
    request = await _load(session, tenant_id, request_id, lock=True)
    approval = await session.scalar(
        select(LeaveApproval)
        .where(
            LeaveApproval.tenant_id == tenant_id,
            LeaveApproval.leave_request_id == request.id,
            LeaveApproval.level == request.current_level,
        )
        .with_for_update()
    )
    is_hr = Role.HR_ADMIN in actor.roles
    is_approver = (
        approval is not None
        and approval.approver_employee_id is not None
        and approval.approver_employee_id == actor.employee_id
    )
    if not (is_hr or is_approver):
        if await _can_view(session, actor, request):
            raise ForbiddenError(
                "Anda bukan approver untuk level ini.", code="not_current_approver"
            )
        raise NotFoundError("Pengajuan cuti tidak ditemukan.")
    if actor.employee_id is not None and actor.employee_id == request.employee_id:
        raise ForbiddenError(
            "Tidak bisa memutuskan pengajuan cuti sendiri.", code="cannot_decide_own_request"
        )
    rules.ensure_pending(LeaveRequestStatus(request.status))
    if approval is None:  # pragma: no cover - baris approval selalu dibuat saat submit
        raise NotFoundError("Data approval tidak ditemukan.")

    now = datetime.now(UTC)
    approval.status = data.decision
    approval.note = data.note.strip() if data.note else None
    approval.decided_by_user_id = actor.user_id
    approval.decided_at = now

    if data.decision == "rejected":
        request.status = LeaveRequestStatus.REJECTED
        request.decided_at = now
        await _release_balance(session, request, pending=-request.days)
        await _skip_remaining(session, request)
    elif request.current_level < request.approval_levels:
        request.current_level += 1
    else:
        request.status = LeaveRequestStatus.APPROVED
        request.decided_at = now
        await _release_balance(session, request, pending=-request.days, used=request.days)

    await session.flush()
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action=f"leave_request.{data.decision}",
        entity_type="leave_request",
        entity_id=request.id,
        after={
            "level": approval.level,
            "status": request.status,
            "note": approval.note,
            "as_hr_override": is_hr and not is_approver,
        },
    )
    return await _detail(session, tenant_id, request.id)


async def cancel(
    session: AsyncSession, actor: AccessClaims, request_id: UUID, data: LeaveCancel
) -> LeaveRequestDetail:
    """Pemohon atau HR. Pending: saldo pending dikembalikan. Approved dan belum mulai: saldo
    terpakai dikembalikan."""
    tenant_id = actor.tenant_id
    request = await _load(session, tenant_id, request_id, lock=True)
    is_owner = actor.employee_id is not None and actor.employee_id == request.employee_id
    if not (is_owner or Role.HR_ADMIN in actor.roles):
        if await _can_view(session, actor, request):
            raise ForbiddenError(
                "Hanya pemohon atau HR yang bisa membatalkan.", code="cannot_cancel_others"
            )
        raise NotFoundError("Pengajuan cuti tidak ditemukan.")

    status = LeaveRequestStatus(request.status)
    rules.ensure_can_cancel(
        status=status, start=request.start_date, today=await tenant_today(session, tenant_id)
    )
    if status == LeaveRequestStatus.PENDING:
        await _release_balance(session, request, pending=-request.days)
    else:
        await _release_balance(session, request, used=-request.days)
    request.status = LeaveRequestStatus.CANCELLED
    request.cancelled_at = datetime.now(UTC)
    await _skip_remaining(session, request)
    await session.flush()
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="leave_request.cancel",
        entity_type="leave_request",
        entity_id=request.id,
        before={"status": status},
        after={"status": request.status, "note": data.note},
    )
    return await _detail(session, tenant_id, request.id)
