"""CLI admin. Memakai MIGRATION_DATABASE_URL (owner), jadi hanya untuk operator, bukan user.

Contoh:
    python -m app.cli create-db-user
    python -m app.cli create-tenant --slug acme --name "PT Acme" --admin-email hr@acme.co.id
    python -m app.cli create-user --tenant acme --email budi@acme.co.id --role employee
    python -m app.cli seed-dev
"""

import argparse
import asyncio
import getpass
import sys
from collections.abc import Awaitable, Callable
from datetime import date, timedelta
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import admin_engine
from app.core.errors import AppError
from app.core.security import AccessClaims
from app.jobs import perf_seed
from app.models import AppUser, Employee, LeavePolicy, LeaveType, OrgUnit, Role
from app.schemas.employee import EmployeeCreate, JobFields
from app.schemas.leave import HolidayCreate, LeavePolicyCreate, LeaveRequestInput, LeaveTypeCreate
from app.schemas.org_unit import OrgUnitCreate
from app.services import admin, employees, jobs, leave_config, leave_requests, org_units
from app.services.tenant import tenant_today

DEV_TENANT_SLUG = "demo"
DEV_PASSWORD = "demo-password"  # noqa: S105 - hanya untuk data dev, ditolak di production
DEV_USERS = (
    ("hr@demo.test", [Role.HR_ADMIN, Role.EMPLOYEE]),
    ("atasan@demo.test", [Role.MANAGER, Role.EMPLOYEE]),
    ("karyawan@demo.test", [Role.EMPLOYEE]),
)
# (kode, nama, kode parent)
DEV_ORG_UNITS = (
    ("DIREKSI", "Direksi", None),
    ("HR", "Human Resources", "DIREKSI"),
    ("ENG", "Engineering", "DIREKSI"),
)
# (nomor, nama, email akun, unit, jabatan, grade, nomor atasan)
DEV_EMPLOYEES = (
    ("D-001", "Hana Rahmawati", "hr@demo.test", "HR", "HR Manager", "G6", None),
    ("D-002", "Andi Pratama", "atasan@demo.test", "ENG", "Engineering Manager", "G6", None),
    ("D-003", "Budi Santoso", "karyawan@demo.test", "ENG", "Software Engineer", "G3", "D-002"),
)
DEV_HIRE_DATE = date(2024, 1, 2)
DEV_LEAVE_TYPES = (
    LeaveTypeCreate(code="CUTI_TAHUNAN", name="Cuti Tahunan", min_notice_days=3),
    LeaveTypeCreate(code="SAKIT", name="Sakit", requires_balance=False, allow_backdated=True),
    LeaveTypeCreate(
        code="MENIKAH",
        name="Cuti Menikah",
        requires_balance=False,
        min_notice_days=14,
        max_days_per_request=3,
    ),
)
# Hari libur bertanggal tetap. Libur keagamaan (Idulfitri, Nyepi, dst.) diinput HR sesuai SKB.
DEV_FIXED_HOLIDAYS = (
    (1, 1, "Tahun Baru Masehi"),
    (5, 1, "Hari Buruh Internasional"),
    (6, 1, "Hari Lahir Pancasila"),
    (8, 17, "Hari Kemerdekaan RI"),
    (12, 25, "Hari Raya Natal"),
)
DEV_LEAVE_KEY = "seed-dev-demo-leave"


async def _in_admin_tx(
    settings: Settings, action: Callable[[AsyncSession], Awaitable[None]]
) -> None:
    engine = admin_engine(settings)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            await action(session)
    finally:
        await engine.dispose()


def _password(value: str | None, prompt: str) -> str:
    return value or getpass.getpass(prompt)


async def create_db_user(settings: Settings, _: argparse.Namespace) -> None:
    if settings.app_db_password is None:
        raise SystemExit("APP_DB_PASSWORD wajib diisi")
    password = settings.app_db_password.get_secret_value()

    async def action(session: AsyncSession) -> None:
        await admin.ensure_app_db_user(session, username=settings.app_db_user, password=password)

    await _in_admin_tx(settings, action)
    print(f"User DB aplikasi '{settings.app_db_user}' siap (anggota role hrus_app).")


async def create_tenant(settings: Settings, args: argparse.Namespace) -> None:
    password = _password(args.admin_password, "Password admin HR: ")

    async def action(session: AsyncSession) -> None:
        tenant = await admin.create_tenant(session, slug=args.slug, name=args.name)
        await admin.create_user(
            session,
            tenant_id=tenant.id,
            email=args.admin_email,
            password=password,
            roles=[Role.HR_ADMIN, Role.EMPLOYEE],
        )
        print(f"Tenant '{tenant.slug}' dibuat ({tenant.id}), admin HR: {args.admin_email}")

    await _in_admin_tx(settings, action)


async def create_user(settings: Settings, args: argparse.Namespace) -> None:
    password = _password(args.password, "Password: ")

    async def action(session: AsyncSession) -> None:
        tenant = await admin.get_tenant_by_slug(session, args.tenant)
        if tenant is None:
            raise SystemExit(f"Tenant '{args.tenant}' tidak ditemukan")
        await admin.create_user(
            session,
            tenant_id=tenant.id,
            email=args.email,
            password=password,
            roles=[Role(r) for r in args.role],
        )
        print(f"User {args.email} dibuat di tenant '{tenant.slug}' dengan role {args.role}")

    await _in_admin_tx(settings, action)


async def _seed_dev_core_hr(session: AsyncSession, tenant_id: UUID) -> dict[str, AppUser]:
    """Unit organisasi + karyawan demo, lalu tiap akun demo ditautkan ke karyawannya."""
    users = {
        user.email: user
        for user in await session.scalars(select(AppUser).where(AppUser.tenant_id == tenant_id))
    }
    hr_user = users["hr@demo.test"]
    actor = AccessClaims(
        user_id=hr_user.id,
        tenant_id=tenant_id,
        roles=frozenset({Role.HR_ADMIN}),
        employee_id=None,
    )

    units = {
        unit.code: unit.id
        for unit in await session.scalars(select(OrgUnit).where(OrgUnit.tenant_id == tenant_id))
    }
    for code, name, parent in DEV_ORG_UNITS:
        if code not in units:
            created = await org_units.create_org_unit(
                session,
                actor,
                OrgUnitCreate(code=code, name=name, parent_id=units.get(parent or "")),
            )
            units[code] = created.id

    numbers = {
        emp.employee_number: emp.id
        for emp in await session.scalars(select(Employee).where(Employee.tenant_id == tenant_id))
    }
    for number, name, email, unit, title, grade, boss in DEV_EMPLOYEES:
        if number not in numbers:
            created_emp = await employees.create_employee(
                session,
                actor,
                EmployeeCreate(
                    employee_number=number,
                    full_name=name,
                    work_email=email,
                    hire_date=DEV_HIRE_DATE,
                    job=JobFields(
                        job_title=title,
                        grade=grade,
                        org_unit_id=units[unit],
                        supervisor_employee_id=numbers.get(boss or ""),
                    ),
                ),
            )
            numbers[number] = created_emp.id
        users[email].employee_id = numbers[number]
    return users


def _claims(user: AppUser, *roles: Role) -> AccessClaims:
    return AccessClaims(
        user_id=user.id,
        tenant_id=user.tenant_id,
        roles=frozenset(roles),
        employee_id=user.employee_id,
    )


async def _seed_dev_leave(session: AsyncSession, users: dict[str, AppUser]) -> None:
    """Tipe cuti, policy 12 hari (masa kerja 12 bulan), hari libur tetap tahun ini dan depan,
    plus satu pengajuan karyawan yang menunggu approval atasan."""
    hr = _claims(users["hr@demo.test"], Role.HR_ADMIN)
    tenant_id = hr.tenant_id
    types = {
        t.code: t.id
        for t in await session.scalars(select(LeaveType).where(LeaveType.tenant_id == tenant_id))
    }
    for leave_type in DEV_LEAVE_TYPES:
        if leave_type.code not in types:
            types[leave_type.code] = (
                await leave_config.create_leave_type(session, hr, leave_type)
            ).id
    has_policy = await session.scalar(select(exists().where(LeavePolicy.tenant_id == tenant_id)))
    if not has_policy:
        await leave_config.create_policy(
            session,
            hr,
            LeavePolicyCreate(
                leave_type_id=types["CUTI_TAHUNAN"],
                min_service_months=12,
                annual_days=12,
                max_carry_over_days=6,
            ),
        )

    today = await tenant_today(session, tenant_id)
    years = (today.year, today.year + 1)
    existing = await leave_config.holidays_between(
        session, tenant_id, date(years[0], 1, 1), date(years[-1], 12, 31)
    )
    for year in years:
        for month, day, name in DEV_FIXED_HOLIDAYS:
            if date(year, month, day) not in existing:
                await leave_config.create_holiday(
                    session, hr, HolidayCreate(holiday_date=date(year, month, day), name=name)
                )

    start = today + timedelta(days=14)
    start += timedelta(days=(7 - start.weekday()) % 7)  # Senin
    try:
        await leave_requests.submit(
            session,
            _claims(users["karyawan@demo.test"], Role.EMPLOYEE),
            LeaveRequestInput(
                leave_type_id=types["CUTI_TAHUNAN"],
                start_date=start,
                end_date=start + timedelta(days=2),
                reason="Liburan keluarga",
            ),
            idempotency_key=DEV_LEAVE_KEY,
        )
    except AppError as exc:  # misal saldo sudah terpakai dari percobaan manual
        print(f"Pengajuan cuti demo dilewati: {exc.message}")


async def seed_dev(settings: Settings, _: argparse.Namespace) -> None:
    if settings.app_env == "production":
        raise SystemExit("seed-dev tidak boleh dijalankan di production")

    async def action(session: AsyncSession) -> None:
        tenant = await admin.get_tenant_by_slug(session, DEV_TENANT_SLUG)
        if tenant is None:
            tenant = await admin.create_tenant(session, slug=DEV_TENANT_SLUG, name="PT Demo")
        # Koneksi admin melewati RLS, jadi filter tenant_id wajib ditulis eksplisit.
        existing = set(
            await session.scalars(select(AppUser.email).where(AppUser.tenant_id == tenant.id))
        )
        for email, roles in DEV_USERS:
            if email not in existing:
                await admin.create_user(
                    session, tenant_id=tenant.id, email=email, password=DEV_PASSWORD, roles=roles
                )
        await jobs.ensure_default_schedules(session, tenant.id, tenant.timezone)
        users = await _seed_dev_core_hr(session, tenant.id)
        await _seed_dev_leave(session, users)

    await _in_admin_tx(settings, action)
    print(f"Tenant dev '{DEV_TENANT_SLUG}' siap. Password semua user: {DEV_PASSWORD}")
    for email, roles in DEV_USERS:
        print(f"  {email:<22} {', '.join(roles)}")


PERF_TENANT_SLUG = "perf"


async def seed_perf(settings: Settings, args: argparse.Namespace) -> None:
    if settings.app_env == "production":
        raise SystemExit("seed-perf tidak boleh dijalankan di production")

    async def action(session: AsyncSession) -> None:
        tenant = await admin.get_tenant_by_slug(session, PERF_TENANT_SLUG)
        if tenant is None:
            tenant = await admin.create_tenant(
                session, slug=PERF_TENANT_SLUG, name="PT Uji Performa"
            )
        await jobs.ensure_default_schedules(session, tenant.id, tenant.timezone)
        has_data = await session.scalar(
            select(func.count()).select_from(Employee).where(Employee.tenant_id == tenant.id)
        )
        if has_data and not args.reset:
            print(f"Tenant '{PERF_TENANT_SLUG}' sudah berisi data. Pakai --reset untuk ulang.")
            return
        if has_data:
            await perf_seed.reset_core_hr(session, tenant.id)
        result = await perf_seed.seed_core_hr(
            session, tenant.id, employees=args.employees, years=args.years
        )
        leave = await perf_seed.seed_leave(session, tenant.id, years=args.years)
        print(
            f"Tenant '{PERF_TENANT_SLUG}' ({tenant.id}): {result.org_units} unit, "
            f"{result.employees} karyawan, {result.jobs} baris riwayat jabatan, "
            f"{leave.requests} pengajuan cuti, {leave.balances} saldo."
        )

    await _in_admin_tx(settings, action)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="CLI admin HRIS")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("create-db-user", help="Buat/perbarui user DB aplikasi (APP_DB_USER)")

    p = sub.add_parser("create-tenant", help="Buat tenant baru + user admin HR")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--admin-email", required=True)
    p.add_argument("--admin-password", help="Kosongkan untuk diminta lewat prompt")

    p = sub.add_parser("create-user", help="Buat user di tenant yang sudah ada")
    p.add_argument("--tenant", required=True, help="Slug tenant")
    p.add_argument("--email", required=True)
    p.add_argument("--role", action="append", required=True, choices=[r.value for r in Role])
    p.add_argument("--password", help="Kosongkan untuk diminta lewat prompt")

    sub.add_parser("seed-dev", help="Tenant demo + user HR, atasan, karyawan (dev saja)")

    p = sub.add_parser("seed-perf", help="Tenant 'perf' untuk uji performa (dev/staging saja)")
    p.add_argument("--employees", type=int, default=7000)
    p.add_argument("--years", type=int, default=3)
    p.add_argument("--reset", action="store_true", help="Hapus lalu buat ulang data tenant perf")
    return parser


COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], Awaitable[None]]] = {
    "create-db-user": create_db_user,
    "create-tenant": create_tenant,
    "create-user": create_user,
    "seed-dev": seed_dev,
    "seed-perf": seed_perf,
}


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        asyncio.run(COMMANDS[args.command](get_settings(), args))
    except ValueError as exc:
        print(f"Gagal: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
