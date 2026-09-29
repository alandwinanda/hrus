"""Report builder manual (ADR 012): jalankan spesifikasi laporan, export, dan template.

Hanya HR. Query berjalan di semantic views dengan statement_timeout, dan setiap export data
karyawan dicatat di audit_log.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import delete, exists, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, RuleViolationError
from app.core.pagination import paginate
from app.core.security import AccessClaims
from app.models import ReportTemplate
from app.reports import export
from app.reports.catalog import DATASETS
from app.reports.query import (
    AGGREGATES_BY_TYPE,
    OPS_BY_TYPE,
    CompiledReport,
    compile_query,
    to_json_value,
)
from app.schemas.common import Page
from app.schemas.reports import (
    ExportFormat,
    ReportColumnRead,
    ReportDatasetRead,
    ReportQuery,
    ReportResult,
    ReportTemplateCreate,
    ReportTemplateRead,
    ReportTemplateUpdate,
)
from app.services.audit import record_audit
from app.services.tenant import tenant_today

STATEMENT_TIMEOUT = "10s"
EXPORT_LIMIT = 10_000
MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass(frozen=True, slots=True)
class ExportFile:
    content: bytes
    filename: str
    media_type: str


def list_datasets() -> list[ReportDatasetRead]:
    return [
        ReportDatasetRead(
            key=d.key,
            label=d.label,
            description=d.description,
            default_period_column=d.default_period_column,
            columns=[
                ReportColumnRead(
                    key=c.key,
                    label=c.label,
                    type=c.type,
                    description=c.description,
                    enum_values=dict(c.enum_values) or None,
                    operators=list(OPS_BY_TYPE[c.type]),
                    aggregates=list(AGGREGATES_BY_TYPE[c.type]),
                )
                for c in d.columns
            ],
        )
        for d in DATASETS.values()
    ]


async def _compile(session: AsyncSession, tenant_id: UUID, spec: ReportQuery) -> CompiledReport:
    return compile_query(spec, tenant_id, await tenant_today(session, tenant_id))


async def _fetch(
    session: AsyncSession, compiled: CompiledReport, limit: int
) -> tuple[list[tuple[Any, ...]], bool]:
    """Ambil maksimal `limit` baris (+1 untuk tahu apakah terpotong), dengan timeout."""
    await session.execute(text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
    try:
        result = await session.execute(compiled.stmt.limit(limit + 1))
    except DBAPIError as exc:
        if "statement timeout" in str(exc.orig):
            raise RuleViolationError(
                "Laporan terlalu berat. Persempit filter atau kurangi kolom.",
                code="report_timeout",
            ) from exc
        raise
    rows = [tuple(row) for row in result.all()]
    return rows[:limit], len(rows) > limit


async def run_query(session: AsyncSession, actor: AccessClaims, spec: ReportQuery) -> ReportResult:
    compiled = await _compile(session, actor.tenant_id, spec)
    rows, truncated = await _fetch(session, compiled, spec.limit)
    return ReportResult(
        columns=compiled.columns,
        rows=[[to_json_value(v) for v in row] for row in rows],
        row_count=len(rows),
        truncated=truncated,
        definition=compiled.definition,
    )


async def export_report(
    session: AsyncSession,
    actor: AccessClaims,
    spec: ReportQuery,
    fmt: ExportFormat,
    *,
    template: ReportTemplate | None = None,
) -> ExportFile:
    """Export sinkron sampai EXPORT_LIMIT baris. Lebih dari itu ditolak, tidak dipotong."""
    compiled = await _compile(session, actor.tenant_id, spec)
    rows, truncated = await _fetch(session, compiled, EXPORT_LIMIT)
    if truncated:
        raise RuleViolationError(
            f"Hasil lebih dari {EXPORT_LIMIT:_} baris. Persempit filter sebelum export.".replace(
                "_", "."
            ),
            code="report_too_large",
        )
    title = template.name if template else compiled.dataset.label
    if fmt == "csv":
        content = export.to_csv(compiled.columns, compiled.sources, rows)
    else:
        content = export.to_xlsx(
            title, compiled.columns, compiled.sources, rows, compiled.definition
        )
    today = await tenant_today(session, actor.tenant_id)
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="report.export",
        entity_type="report_template" if template else "report",
        entity_id=template.id if template else None,
        after={
            "dataset": spec.dataset,
            "columns": spec.columns,
            "filters": [f.model_dump(mode="json") for f in spec.filters],
            "aggregates": [a.model_dump(mode="json") for a in spec.aggregates],
            "format": fmt,
            "rows": len(rows),
        },
    )
    return ExportFile(
        content=content,
        filename=f"laporan-{compiled.dataset.key}-{today.isoformat()}.{fmt}",
        media_type=MEDIA_TYPES[fmt],
    )


# --- Template ---------------------------------------------------------------------------


def _read(template: ReportTemplate) -> ReportTemplateRead:
    return ReportTemplateRead.model_validate(template)


async def _get(session: AsyncSession, tenant_id: UUID, template_id: UUID) -> ReportTemplate:
    template = await session.scalar(
        select(ReportTemplate).where(
            ReportTemplate.tenant_id == tenant_id, ReportTemplate.id == template_id
        )
    )
    if template is None:
        raise NotFoundError("Template laporan tidak ditemukan.")
    return template


def _stored_query(template: ReportTemplate) -> ReportQuery:
    try:
        return ReportQuery.model_validate(template.query)
    except ValidationError as exc:
        raise RuleViolationError(
            "Template ini tidak valid lagi (katalog laporan berubah). Perbarui template.",
            code="report_template_invalid",
        ) from exc


async def _ensure_name_free(
    session: AsyncSession, tenant_id: UUID, name: str, exclude: UUID | None = None
) -> None:
    stmt = exists().where(ReportTemplate.tenant_id == tenant_id, ReportTemplate.name == name)
    if exclude is not None:
        stmt = stmt.where(ReportTemplate.id != exclude)
    if await session.scalar(select(stmt)):
        raise ConflictError(f"Template '{name}' sudah ada.", code="report_template_name_taken")


async def list_templates(
    session: AsyncSession, tenant_id: UUID, *, cursor: str | None, limit: int
) -> Page[ReportTemplateRead]:
    stmt = select(ReportTemplate).where(ReportTemplate.tenant_id == tenant_id)
    page = await paginate(session, stmt, [ReportTemplate.name, ReportTemplate.id], cursor, limit)
    return Page(items=[_read(t) for t in page.items], next_cursor=page.next_cursor)


async def get_template(
    session: AsyncSession, tenant_id: UUID, template_id: UUID
) -> ReportTemplateRead:
    return _read(await _get(session, tenant_id, template_id))


async def create_template(
    session: AsyncSession,
    actor: AccessClaims,
    data: ReportTemplateCreate,
    *,
    idempotency_key: str | None = None,
) -> ReportTemplateRead:
    """Spesifikasi divalidasi (dikompilasi) dulu. Idempotency-Key yang sama mengembalikan
    template yang sudah ada, supaya simpan dari chat tidak dobel."""
    tenant_id = actor.tenant_id
    if idempotency_key:
        existing = await session.scalar(
            select(ReportTemplate).where(
                ReportTemplate.tenant_id == tenant_id,
                ReportTemplate.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            return _read(existing)
    await _compile(session, tenant_id, data.query)
    name = data.name.strip()
    await _ensure_name_free(session, tenant_id, name)
    template = ReportTemplate(
        tenant_id=tenant_id,
        name=name,
        description=data.description,
        query=data.query.model_dump(mode="json"),
        created_by_user_id=actor.user_id,
        idempotency_key=idempotency_key,
    )
    try:
        async with session.begin_nested():
            session.add(template)
            await session.flush()
    except IntegrityError as exc:
        raise ConflictError(
            f"Template '{name}' sudah ada.", code="report_template_name_taken"
        ) from exc
    await session.refresh(template)
    result = _read(template)
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="report_template.create",
        entity_type="report_template",
        entity_id=template.id,
        after=result.model_dump(mode="json", include={"name", "description", "query"}),
    )
    return result


async def update_template(
    session: AsyncSession, actor: AccessClaims, template_id: UUID, data: ReportTemplateUpdate
) -> ReportTemplateRead:
    template = await _get(session, actor.tenant_id, template_id)
    fields = {"name", "description", "query"}
    before = _read(template).model_dump(mode="json", include=fields)
    if data.name is not None:
        name = data.name.strip()
        await _ensure_name_free(session, actor.tenant_id, name, exclude=template.id)
        template.name = name
    if "description" in data.model_fields_set:
        template.description = data.description
    if data.query is not None:
        await _compile(session, actor.tenant_id, data.query)
        template.query = data.query.model_dump(mode="json")
    template.updated_by_user_id = actor.user_id
    await session.flush()
    await session.refresh(template)
    result = _read(template)
    after = result.model_dump(mode="json", include=fields)
    if after != before:
        await record_audit(
            session,
            tenant_id=actor.tenant_id,
            actor_user_id=actor.user_id,
            action="report_template.update",
            entity_type="report_template",
            entity_id=template.id,
            before=before,
            after=after,
        )
    return result


async def delete_template(session: AsyncSession, actor: AccessClaims, template_id: UUID) -> None:
    template = await _get(session, actor.tenant_id, template_id)
    before = _read(template).model_dump(mode="json", include={"name", "description", "query"})
    await session.execute(
        delete(ReportTemplate).where(
            ReportTemplate.tenant_id == actor.tenant_id, ReportTemplate.id == template.id
        )
    )
    await record_audit(
        session,
        tenant_id=actor.tenant_id,
        actor_user_id=actor.user_id,
        action="report_template.delete",
        entity_type="report_template",
        entity_id=template_id,
        before=before,
    )


async def run_template(
    session: AsyncSession, actor: AccessClaims, template_id: UUID
) -> ReportResult:
    template = await _get(session, actor.tenant_id, template_id)
    return await run_query(session, actor, _stored_query(template))


async def export_template(
    session: AsyncSession, actor: AccessClaims, template_id: UUID, fmt: ExportFormat
) -> ExportFile:
    template = await _get(session, actor.tenant_id, template_id)
    return await export_report(session, actor, _stored_query(template), fmt, template=template)
