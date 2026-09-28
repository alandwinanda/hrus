from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, status

from app.api.deps import CurrentUserDep, TenantSessionDep, require_roles
from app.api.pagination import PageParamsDep
from app.core.logging import get_logger
from app.jobs.dispatch import Dispatch, Task, get_dispatch
from app.models import JobRunStatus, JobTrigger, Role
from app.schemas.common import Page
from app.schemas.jobs import (
    JobDefinitionRead,
    JobRunChunkRead,
    JobRunCreate,
    JobRunLogRead,
    JobRunRead,
    JobScheduleRead,
    JobScheduleUpdate,
)
from app.services import jobs as service

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = get_logger(__name__)

HrAdmin = Annotated[object, Depends(require_roles(Role.HR_ADMIN))]
DispatchDep = Annotated[Dispatch, Depends(get_dispatch)]


def _send(dispatch: Dispatch, tasks: list[Task]) -> None:
    """Dipanggil setelah response (transaksi sudah commit). Kalau broker gagal, run tetap
    tercatat dan scheduler mengirim ulang."""
    for task in tasks:
        try:
            dispatch(task)
        except Exception:
            logger.exception("job_dispatch_failed", task=task.name, args=task.args)


@router.get("/definitions")
async def list_job_definitions(_: HrAdmin) -> list[JobDefinitionRead]:
    """Job yang tersedia beserta skema parameternya."""
    return service.list_definitions()


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def run_job(
    body: JobRunCreate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    background: BackgroundTasks,
    dispatch: DispatchDep,
    _: HrAdmin,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=8, max_length=100)
    ] = None,
) -> JobRunRead:
    """MCP tool: run_job (wajib konfirmasi user). Hanya mencatat run lalu antre ke worker.
    Pakai dry_run=true untuk melihat hasilnya dulu tanpa mengubah data."""
    run, tasks = await service.create_run(
        session,
        user.tenant_id,
        job_code=body.job_code,
        params=body.params,
        dry_run=body.dry_run,
        trigger=JobTrigger.USER,
        actor_user_id=user.user_id,
        idempotency_key=idempotency_key,
    )
    background.add_task(_send, dispatch, tasks)
    return JobRunRead.model_validate(run)


@router.get("/runs")
async def list_job_runs(
    user: CurrentUserDep,
    session: TenantSessionDep,
    page: PageParamsDep,
    _: HrAdmin,
    job_code: str | None = None,
    run_status: Annotated[JobRunStatus | None, Query(alias="status")] = None,
    year: Annotated[int | None, Query(ge=2000, le=2100)] = None,
) -> Page[JobRunRead]:
    return await service.list_runs(
        session,
        user.tenant_id,
        job_code=job_code,
        status=run_status,
        year=year,
        cursor=page.cursor,
        limit=page.limit,
    )


@router.get("/runs/{run_id}")
async def get_job_run(
    run_id: UUID, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> JobRunRead:
    """MCP tool: get_job_status. Status, progress (chunk), dan ringkasan hasil."""
    return await service.get_run(session, user.tenant_id, run_id)


@router.get("/runs/{run_id}/chunks")
async def list_job_run_chunks(
    run_id: UUID, user: CurrentUserDep, session: TenantSessionDep, page: PageParamsDep, _: HrAdmin
) -> Page[JobRunChunkRead]:
    """Detail per chunk, termasuk daftar perubahan (untuk review hasil dry-run)."""
    return await service.list_chunks(
        session, user.tenant_id, run_id, cursor=page.cursor, limit=page.limit
    )


@router.get("/runs/{run_id}/logs")
async def list_job_run_logs(
    run_id: UUID, user: CurrentUserDep, session: TenantSessionDep, page: PageParamsDep, _: HrAdmin
) -> Page[JobRunLogRead]:
    return await service.list_logs(
        session, user.tenant_id, run_id, cursor=page.cursor, limit=page.limit
    )


@router.post("/runs/{run_id}/cancel")
async def cancel_job_run(
    run_id: UUID, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> JobRunRead:
    return await service.cancel_run(session, user, run_id)


@router.post("/runs/{run_id}/retry")
async def retry_job_run(
    run_id: UUID,
    user: CurrentUserDep,
    session: TenantSessionDep,
    background: BackgroundTasks,
    dispatch: DispatchDep,
    _: HrAdmin,
) -> JobRunRead:
    """Ulangi chunk yang gagal saja (restart dari titik gagal)."""
    run, tasks = await service.retry_run(session, user, run_id)
    background.add_task(_send, dispatch, tasks)
    return run


@router.get("/schedules")
async def list_job_schedules(
    user: CurrentUserDep, session: TenantSessionDep, page: PageParamsDep, _: HrAdmin
) -> Page[JobScheduleRead]:
    return await service.list_schedules(
        session, user.tenant_id, cursor=page.cursor, limit=page.limit
    )


@router.patch("/schedules/{schedule_id}")
async def update_job_schedule(
    schedule_id: UUID,
    body: JobScheduleUpdate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
) -> JobScheduleRead:
    """Ubah jam (cron), zona waktu, parameter, atau nonaktifkan jadwal tanpa deploy."""
    return await service.update_schedule(session, user, schedule_id, body)
