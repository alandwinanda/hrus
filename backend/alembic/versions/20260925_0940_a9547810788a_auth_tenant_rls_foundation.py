"""auth tenant rls foundation

Revision ID: a9547810788a
Revises:
Create Date: 2026-09-25 09:40:38.079036

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.tenant import (
    APP_DB_ROLE,
    create_app_role_statement,
    grant_statement,
    rls_statements,
)

# revision identifiers, used by Alembic.
revision: str = "a9547810788a"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenant",
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), server_default="Asia/Jakarta", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tenant")),
        sa.UniqueConstraint("slug", name=op.f("uq_tenant_slug")),
    )
    op.create_table(
        "app_user",
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_app_user_email_lowercase")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_app_user_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
    )
    op.create_index("uq_app_user_tenant_id_email", "app_user", ["tenant_id", "email"], unique=True)
    op.create_table(
        "audit_log",
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("request_id", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_audit_log_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index(
        "ix_audit_log_created_at_brin",
        "audit_log",
        ["created_at"],
        unique=False,
        postgresql_using="brin",
    )
    op.create_index(
        "ix_audit_log_tenant_id_entity",
        "audit_log",
        ["tenant_id", "entity_type", "entity_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "refresh_token",
        sa.Column("app_user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["app_user_id"],
            ["app_user.id"],
            name=op.f("fk_refresh_token_app_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_token.id"],
            name=op.f("fk_refresh_token_replaced_by_id_refresh_token"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_refresh_token_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_refresh_token")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_refresh_token_token_hash")),
    )
    op.create_index(
        "ix_refresh_token_tenant_id_app_user_id",
        "refresh_token",
        ["tenant_id", "app_user_id"],
        unique=False,
    )
    op.create_table(
        "user_role",
        sa.Column("app_user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "role IN ('employee', 'manager', 'hr_admin')", name=op.f("ck_user_role_role_valid")
        ),
        sa.ForeignKeyConstraint(
            ["app_user_id"],
            ["app_user.id"],
            name=op.f("fk_user_role_app_user_id_app_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_user_role_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("app_user_id", "role", name=op.f("pk_user_role")),
    )

    # Role grup aplikasi (tanpa BYPASSRLS).
    # User login dibuat terpisah: `python -m app.cli create-db-user`.
    op.execute(create_app_role_statement())
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_DB_ROLE}")

    # tenant: tabel induk tanpa RLS, aplikasi hanya membaca. Tenant baru dibuat lewat CLI admin.
    op.execute(grant_statement("tenant", "SELECT"))
    # app_user tidak dihapus, cukup dinonaktifkan (is_active = false).
    op.execute(grant_statement("app_user", "SELECT, INSERT, UPDATE"))
    op.execute(grant_statement("user_role", "SELECT, INSERT, DELETE"))
    op.execute(grant_statement("refresh_token", "SELECT, INSERT, UPDATE, DELETE"))
    # audit_log append-only.
    op.execute(grant_statement("audit_log", "SELECT, INSERT"))

    for table in ("app_user", "user_role", "refresh_token", "audit_log"):
        for statement in rls_statements(table):
            op.execute(statement)


def downgrade() -> None:
    # Role grup hrus_app sengaja tidak dihapus: bisa dipakai database lain di cluster yang sama.
    op.drop_table("user_role")
    op.drop_index("ix_refresh_token_tenant_id_app_user_id", table_name="refresh_token")
    op.drop_table("refresh_token")
    op.drop_index("ix_audit_log_tenant_id_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at_brin", table_name="audit_log", postgresql_using="brin")
    op.drop_table("audit_log")
    op.drop_index("uq_app_user_tenant_id_email", table_name="app_user")
    op.drop_table("app_user")
    op.drop_table("tenant")
