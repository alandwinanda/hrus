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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.db import admin_engine
from app.models import AppUser, Role
from app.services import admin

DEV_TENANT_SLUG = "demo"
DEV_PASSWORD = "demo-password"  # noqa: S105 - hanya untuk data dev, ditolak di production
DEV_USERS = (
    ("hr@demo.test", [Role.HR_ADMIN, Role.EMPLOYEE]),
    ("atasan@demo.test", [Role.MANAGER, Role.EMPLOYEE]),
    ("karyawan@demo.test", [Role.EMPLOYEE]),
)


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

    await _in_admin_tx(settings, action)
    print(f"Tenant dev '{DEV_TENANT_SLUG}' siap. Password semua user: {DEV_PASSWORD}")
    for email, roles in DEV_USERS:
        print(f"  {email:<22} {', '.join(roles)}")


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
    return parser


COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], Awaitable[None]]] = {
    "create-db-user": create_db_user,
    "create-tenant": create_tenant,
    "create-user": create_user,
    "seed-dev": seed_dev,
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
