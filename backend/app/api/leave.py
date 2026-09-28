from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, status

from app.api.deps import CurrentUserDep, TenantSessionDep, require_roles
from app.api.pagination import PageParamsDep
from app.models import LeaveRequestStatus, Role
from app.schemas.common import Page
from app.schemas.leave import (
    BalanceAdjustment,
    LeaveBalanceRead,
    LeaveBalanceSummary,
    LeaveCancel,
    LeaveDecision,
    LeaveRequestDetail,
    LeaveRequestInput,
    LeaveRequestRead,
    LeaveRequestScope,
    LeaveValidationResult,
    TeamCalendarItem,
)
from app.services import leave_balances, leave_requests

router = APIRouter(prefix="/leave", tags=["leave"])

HrAdmin = Annotated[object, Depends(require_roles(Role.HR_ADMIN))]
ManagerOrHr = Annotated[object, Depends(require_roles(Role.MANAGER, Role.HR_ADMIN))]
Year = Annotated[int | None, Query(ge=2000, le=2100)]


@router.get("/balances")
async def get_leave_balance(
    user: CurrentUserDep,
    session: TenantSessionDep,
    employee_id: UUID | None = None,
    year: Year = None,
) -> LeaveBalanceSummary:
    """Saldo cuti per tipe (MCP tool: get_leave_balance). Default: diri sendiri, tahun berjalan.
    HR bisa melihat siapa saja, atasan bisa melihat bawahan langsung."""
    return await leave_balances.list_balances(session, user, employee_id=employee_id, year=year)


@router.post("/balances/adjustments")
async def adjust_leave_balance(
    body: BalanceAdjustment, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> LeaveBalanceRead:
    """Koreksi saldo manual oleh HR, misal saldo awal dari sistem lama. Tercatat di audit_log."""
    return await leave_balances.adjust_balance(session, user, body)


@router.post("/requests/validate")
async def validate_leave_request(
    body: LeaveRequestInput, user: CurrentUserDep, session: TenantSessionDep
) -> LeaveValidationResult:
    """Dry-run (MCP tool: validate_leave_request). Tidak menyimpan apa pun. Form dan chat
    memakai endpoint ini, jadi aturannya sama persis dengan submit."""
    return await leave_requests.validate(session, user, body)


@router.post("/requests", status_code=status.HTTP_201_CREATED)
async def submit_leave_request(
    body: LeaveRequestInput,
    user: CurrentUserDep,
    session: TenantSessionDep,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=8, max_length=100)
    ] = None,
) -> LeaveRequestDetail:
    """Ajukan cuti (MCP tool: submit_leave_request, wajib konfirmasi user). Idempotency-Key
    yang sama mengembalikan pengajuan yang sudah ada, jadi retry tidak membuat dobel."""
    return await leave_requests.submit(session, user, body, idempotency_key=idempotency_key)


@router.get("/requests")
async def list_leave_requests(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    scope: LeaveRequestScope = "mine",
    request_status: Annotated[LeaveRequestStatus | None, Query(alias="status")] = None,
    year: Year = None,
) -> Page[LeaveRequestRead]:
    """MCP tool: list_leave_requests. mine: pengajuan sendiri. approvals: menunggu keputusan
    saya. all: semua pengajuan (HR). Default tahun berjalan, kecuali approvals."""
    return await leave_requests.list_requests(
        session,
        user,
        scope=scope,
        status=request_status,
        year=year,
        cursor=page.cursor,
        limit=page.limit,
    )


@router.get("/requests/{request_id}")
async def get_leave_request(
    request_id: UUID, user: CurrentUserDep, session: TenantSessionDep
) -> LeaveRequestDetail:
    """Pemohon, approver pengajuan itu, dan HR."""
    return await leave_requests.get_request(session, user, request_id)


@router.post("/requests/{request_id}/cancel")
async def cancel_leave_request(
    request_id: UUID, body: LeaveCancel, user: CurrentUserDep, session: TenantSessionDep
) -> LeaveRequestDetail:
    """MCP tool: cancel_leave_request (wajib konfirmasi user). Pemohon atau HR, sebelum cuti
    dimulai. Saldo dikembalikan di transaksi yang sama."""
    return await leave_requests.cancel(session, user, request_id, body)


@router.post("/requests/{request_id}/decision")
async def decide_leave_request(
    request_id: UUID, body: LeaveDecision, user: CurrentUserDep, session: TenantSessionDep
) -> LeaveRequestDetail:
    """MCP tool: decide_leave_request (wajib konfirmasi user). Approver di level berjalan,
    atau HR untuk level mana pun (termasuk level tanpa atasan)."""
    return await leave_requests.decide(session, user, request_id, body)


@router.get("/team-calendar")
async def get_team_calendar(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    _: ManagerOrHr,
    start: date,
    end: date,
    org_unit_id: UUID | None = None,
) -> Page[TeamCalendarItem]:
    """MCP tool: get_team_calendar. Cuti pending dan approved di rentang tanggal (maks 93 hari).
    Atasan: bawahan langsung. HR: semua, opsional per unit."""
    return await leave_requests.team_calendar(
        session,
        user,
        start=start,
        end=end,
        org_unit_id=org_unit_id,
        cursor=page.cursor,
        limit=page.limit,
    )
