from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, RuleViolationError
from app.core.pagination import paginate
from app.core.security import AccessClaims
from app.models import Employee, OrgUnit
from app.rules import core_hr as rules
from app.schemas.common import Page
from app.schemas.org_unit import OrgUnitCreate, OrgUnitRead, OrgUnitUpdate
from app.services.audit import record_audit

_COLUMNS = (
    OrgUnit.id,
    OrgUnit.code,
    OrgUnit.name,
    OrgUnit.parent_id,
    OrgUnit.manager_employee_id,
    OrgUnit.is_active,
)


async def list_org_units(
    session: AsyncSession,
    tenant_id: UUID,
    *,
    cursor: str | None,
    limit: int,
    include_inactive: bool = False,
) -> Page[OrgUnitRead]:
    stmt = select(*_COLUMNS).where(OrgUnit.tenant_id == tenant_id)
    if not include_inactive:
        stmt = stmt.where(OrgUnit.is_active.is_(True))
    page = await paginate(session, stmt, [OrgUnit.code, OrgUnit.id], cursor, limit)
    return Page(
        items=[OrgUnitRead.model_validate(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


async def _load(session: AsyncSession, tenant_id: UUID, unit_id: UUID) -> OrgUnit:
    unit = await session.scalar(
        select(OrgUnit).where(OrgUnit.tenant_id == tenant_id, OrgUnit.id == unit_id)
    )
    if unit is None:
        raise NotFoundError("Unit organisasi tidak ditemukan.")
    return unit


async def get_org_unit(session: AsyncSession, tenant_id: UUID, unit_id: UUID) -> OrgUnitRead:
    return OrgUnitRead.model_validate(await _load(session, tenant_id, unit_id))


async def ensure_active_org_unit(session: AsyncSession, tenant_id: UUID, unit_id: UUID) -> None:
    active = await session.scalar(
        select(OrgUnit.is_active).where(OrgUnit.tenant_id == tenant_id, OrgUnit.id == unit_id)
    )
    if not active:
        raise RuleViolationError(
            "Unit organisasi tidak ditemukan atau sudah nonaktif.", code="org_unit_invalid"
        )


async def ensure_employee_exists(
    session: AsyncSession, tenant_id: UUID, employee_id: UUID, *, field: str
) -> None:
    found = await session.scalar(
        select(exists().where(Employee.tenant_id == tenant_id, Employee.id == employee_id))
    )
    if not found:
        raise RuleViolationError(f"{field}: karyawan tidak ditemukan.", code="employee_invalid")


async def _ancestors(session: AsyncSession, tenant_id: UUID, start_id: UUID) -> list[UUID]:
    """start_id beserta semua leluhurnya. UNION (bukan UNION ALL) supaya aman dari siklus."""
    chain = (
        select(OrgUnit.id, OrgUnit.parent_id)
        .where(OrgUnit.tenant_id == tenant_id, OrgUnit.id == start_id)
        .cte("chain", recursive=True)
    )
    chain = chain.union(
        select(OrgUnit.id, OrgUnit.parent_id)
        .join(chain, OrgUnit.id == chain.c.parent_id)
        .where(OrgUnit.tenant_id == tenant_id)
    )
    return list(await session.scalars(select(chain.c.id)))


async def create_org_unit(
    session: AsyncSession, actor: AccessClaims, data: OrgUnitCreate
) -> OrgUnitRead:
    tenant_id = actor.tenant_id
    code = data.code.strip().upper()
    taken = await session.scalar(
        select(exists().where(OrgUnit.tenant_id == tenant_id, OrgUnit.code == code))
    )
    if taken:
        raise ConflictError(f"Kode unit '{code}' sudah dipakai.", code="org_unit_code_taken")
    if data.parent_id is not None:
        await ensure_active_org_unit(session, tenant_id, data.parent_id)
    if data.manager_employee_id is not None:
        await ensure_employee_exists(
            session, tenant_id, data.manager_employee_id, field="manager_employee_id"
        )

    unit = OrgUnit(
        tenant_id=tenant_id,
        code=code,
        name=data.name.strip(),
        parent_id=data.parent_id,
        manager_employee_id=data.manager_employee_id,
        is_active=True,
    )
    session.add(unit)
    await session.flush()
    result = OrgUnitRead.model_validate(unit)
    await record_audit(
        session,
        tenant_id=tenant_id,
        actor_user_id=actor.user_id,
        action="org_unit.create",
        entity_type="org_unit",
        entity_id=unit.id,
        after=result.model_dump(mode="json"),
    )
    return result


async def update_org_unit(
    session: AsyncSession, actor: AccessClaims, unit_id: UUID, data: OrgUnitUpdate
) -> OrgUnitRead:
    tenant_id = actor.tenant_id
    unit = await _load(session, tenant_id, unit_id)
    before = OrgUnitRead.model_validate(unit)
    fields = data.model_fields_set

    if "name" in fields and data.name is not None:
        unit.name = data.name.strip()
    if "parent_id" in fields:
        if data.parent_id is not None:
            await ensure_active_org_unit(session, tenant_id, data.parent_id)
            rules.check_org_parent(
                unit_id=unit.id,
                parent_ancestors=await _ancestors(session, tenant_id, data.parent_id),
            )
        unit.parent_id = data.parent_id
    if "manager_employee_id" in fields:
        if data.manager_employee_id is not None:
            await ensure_employee_exists(
                session, tenant_id, data.manager_employee_id, field="manager_employee_id"
            )
        unit.manager_employee_id = data.manager_employee_id
    if "is_active" in fields and data.is_active is not None:
        if not data.is_active:
            has_active_child = await session.scalar(
                select(
                    exists().where(
                        OrgUnit.tenant_id == tenant_id,
                        OrgUnit.parent_id == unit.id,
                        OrgUnit.is_active.is_(True),
                    )
                )
            )
            if has_active_child:
                raise RuleViolationError(
                    "Nonaktifkan atau pindahkan sub-unit terlebih dahulu.",
                    code="org_unit_has_active_children",
                )
        unit.is_active = data.is_active

    await session.flush()
    after = OrgUnitRead.model_validate(unit)
    if after != before:
        await record_audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=actor.user_id,
            action="org_unit.update",
            entity_type="org_unit",
            entity_id=unit.id,
            before=before.model_dump(mode="json"),
            after=after.model_dump(mode="json"),
        )
    return after
