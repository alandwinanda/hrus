from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response, status

from app.api.deps import CurrentUserDep, TenantSessionDep, require_roles
from app.api.pagination import PageParamsDep
from app.models import Role
from app.schemas.common import Page
from app.schemas.reports import (
    ExportFormat,
    ReportDatasetRead,
    ReportQuery,
    ReportResult,
    ReportTemplateCreate,
    ReportTemplateRead,
    ReportTemplateUpdate,
)
from app.services import reports as service
from app.services.reports import ExportFile

router = APIRouter(prefix="/reports", tags=["reports"])

HrAdmin = Annotated[object, Depends(require_roles(Role.HR_ADMIN))]
FormatQuery = Annotated[ExportFormat, Query(alias="format")]


def _download(file: ExportFile) -> Response:
    return Response(
        content=file.content,
        media_type=file.media_type,
        headers={"Content-Disposition": f'attachment; filename="{file.filename}"'},
    )


@router.get("/datasets")
async def list_report_datasets(_: HrAdmin) -> list[ReportDatasetRead]:
    """Dataset, kolom (label, tipe, deskripsi), operator filter, dan agregat yang tersedia."""
    return service.list_datasets()


@router.post("/query")
async def run_report(
    body: ReportQuery, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> ReportResult:
    """MCP tool: run_report. Preview maksimal 500 baris; `definition` menjelaskan filter,
    periode, dan agregasi yang dipakai."""
    return await service.run_query(session, user, body)


@router.post("/export", response_class=Response)
async def export_report(
    body: ReportQuery,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
    fmt: FormatQuery = "xlsx",
) -> Response:
    """Download CSV atau Excel (maksimal 10.000 baris). Setiap export dicatat di audit."""
    return _download(await service.export_report(session, user, body, fmt))


@router.get("/templates")
async def list_report_templates(
    user: CurrentUserDep, session: TenantSessionDep, page: PageParamsDep, _: HrAdmin
) -> Page[ReportTemplateRead]:
    return await service.list_templates(
        session, user.tenant_id, cursor=page.cursor, limit=page.limit
    )


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def save_report_template(
    body: ReportTemplateCreate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", min_length=8, max_length=100)
    ] = None,
) -> ReportTemplateRead:
    """MCP tool: save_report_template (wajib konfirmasi user). Laporan disimpan sebagai
    spesifikasi, jadi dijalankan ulang tanpa LLM dan hasilnya konsisten."""
    return await service.create_template(session, user, body, idempotency_key=idempotency_key)


@router.get("/templates/{template_id}")
async def get_report_template(
    template_id: UUID, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> ReportTemplateRead:
    return await service.get_template(session, user.tenant_id, template_id)


@router.patch("/templates/{template_id}")
async def update_report_template(
    template_id: UUID,
    body: ReportTemplateUpdate,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
) -> ReportTemplateRead:
    return await service.update_template(session, user, template_id, body)


@router.delete("/templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_report_template(
    template_id: UUID, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> None:
    await service.delete_template(session, user, template_id)


@router.post("/templates/{template_id}/run")
async def run_report_template(
    template_id: UUID, user: CurrentUserDep, session: TenantSessionDep, _: HrAdmin
) -> ReportResult:
    return await service.run_template(session, user, template_id)


@router.get("/templates/{template_id}/export", response_class=Response)
async def export_report_template(
    template_id: UUID,
    user: CurrentUserDep,
    session: TenantSessionDep,
    _: HrAdmin,
    fmt: FormatQuery = "xlsx",
) -> Response:
    return _download(await service.export_template(session, user, template_id, fmt))
