"""leave management

Revision ID: 7686c168123e
Revises: bb316c7a18d7
Create Date: 2026-09-28 01:28:42.052204

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.tenant import grant_statement, rls_statements

# revision identifiers, used by Alembic.
revision: str = "7686c168123e"
down_revision: str | Sequence[str] | None = "bb316c7a18d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "holiday_calendar",
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('national', 'collective_leave', 'company')",
            name=op.f("ck_holiday_calendar_kind_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_holiday_calendar_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_holiday_calendar")),
    )
    op.create_index(
        "uq_holiday_calendar_tenant_id_date",
        "holiday_calendar",
        ["tenant_id", "holiday_date"],
        unique=True,
    )
    op.create_table(
        "leave_type",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("requires_balance", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_paid", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("min_notice_days", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("allow_backdated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_days_per_request", sa.SmallInteger(), nullable=True),
        sa.Column("approval_levels", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.CheckConstraint(
            "approval_levels BETWEEN 1 AND 2", name=op.f("ck_leave_type_approval_levels_valid")
        ),
        sa.CheckConstraint(
            "max_days_per_request IS NULL OR max_days_per_request > 0",
            name=op.f("ck_leave_type_max_days_positive"),
        ),
        sa.CheckConstraint("min_notice_days >= 0", name=op.f("ck_leave_type_min_notice_positive")),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_leave_type_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_type")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_leave_type_tenant_id_id"),
    )
    op.create_index(
        "uq_leave_type_tenant_id_code", "leave_type", ["tenant_id", "code"], unique=True
    )
    op.create_table(
        "leave_balance",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("year", sa.SmallInteger(), nullable=False),
        sa.Column("entitled", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("carried_over", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("adjusted", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("used", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("pending", sa.SmallInteger(), server_default="0", nullable=False),
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
        sa.CheckConstraint(
            "used >= 0 AND pending >= 0", name=op.f("ck_leave_balance_usage_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_leave_balance_employee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_type.tenant_id", "leave_type.id"],
            name="fk_leave_balance_leave_type",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_leave_balance_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_balance")),
    )
    op.create_index(
        "uq_leave_balance_key",
        "leave_balance",
        ["tenant_id", "employee_id", "leave_type_id", "year"],
        unique=True,
    )
    op.create_table(
        "leave_policy",
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("grade", sa.String(length=20), nullable=True),
        sa.Column("min_service_months", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("annual_days", sa.SmallInteger(), nullable=False),
        sa.Column("max_carry_over_days", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column(
            "carry_over_expiry_months", sa.SmallInteger(), server_default="3", nullable=False
        ),
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
        sa.CheckConstraint("annual_days >= 0", name=op.f("ck_leave_policy_annual_days_positive")),
        sa.CheckConstraint(
            "max_carry_over_days >= 0", name=op.f("ck_leave_policy_carry_over_positive")
        ),
        sa.CheckConstraint(
            "min_service_months >= 0", name=op.f("ck_leave_policy_min_service_positive")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_type.tenant_id", "leave_type.id"],
            name="fk_leave_policy_leave_type",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_leave_policy_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_policy")),
    )
    op.create_index(
        "uq_leave_policy_rule",
        "leave_policy",
        [
            "tenant_id",
            "leave_type_id",
            sa.literal_column("coalesce(grade, '')"),
            "min_service_months",
        ],
        unique=True,
    )
    op.create_table(
        "leave_request",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("leave_type_id", sa.Uuid(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("days", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("approval_levels", sa.SmallInteger(), nullable=False),
        sa.Column("current_level", sa.SmallInteger(), server_default="1", nullable=False),
        sa.Column("requested_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("validation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name=op.f("ck_leave_request_status_valid"),
        ),
        sa.CheckConstraint(
            "current_level BETWEEN 1 AND approval_levels", name=op.f("ck_leave_request_level_valid")
        ),
        sa.CheckConstraint("days > 0", name=op.f("ck_leave_request_days_positive")),
        sa.CheckConstraint(
            "end_date >= start_date", name=op.f("ck_leave_request_date_range_valid")
        ),
        sa.CheckConstraint(
            "end_date - start_date < 184", name=op.f("ck_leave_request_span_within_limit")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_leave_request_employee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_type_id"],
            ["leave_type.tenant_id", "leave_type.id"],
            name="fk_leave_request_leave_type",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_leave_request_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_request")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_leave_request_tenant_id_id"),
    )
    op.create_index(
        "ix_leave_request_tenant_id_employee_id_start",
        "leave_request",
        ["tenant_id", "employee_id", "start_date"],
        unique=False,
    )
    op.create_index(
        "ix_leave_request_tenant_id_status", "leave_request", ["tenant_id", "status"], unique=False
    )
    op.create_index(
        "ix_leave_request_tenant_id_period",
        "leave_request",
        ["tenant_id", "start_date", "end_date", "id"],
        unique=False,
    )
    op.create_index(
        "uq_leave_request_idempotency",
        "leave_request",
        ["tenant_id", "employee_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "leave_approval",
        sa.Column("leave_request_id", sa.Uuid(), nullable=False),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("approver_employee_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'skipped')",
            name=op.f("ck_leave_approval_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approver_employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_leave_approval_approver",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "leave_request_id"],
            ["leave_request.tenant_id", "leave_request.id"],
            name="fk_leave_approval_request",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_leave_approval_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_leave_approval")),
    )
    op.create_index(
        "ix_leave_approval_inbox",
        "leave_approval",
        ["tenant_id", "approver_employee_id", "status"],
        unique=False,
    )
    op.create_index(
        "uq_leave_approval_level",
        "leave_approval",
        ["tenant_id", "leave_request_id", "level"],
        unique=True,
    )

    # Cegah pengajuan tumpang tindih di level database (termasuk race dua request bersamaan).
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        "ALTER TABLE leave_request ADD CONSTRAINT ex_leave_request_no_overlap "
        "EXCLUDE USING gist (tenant_id WITH =, employee_id WITH =, "
        "daterange(start_date, end_date, '[]') WITH &&) "
        "WHERE (status IN ('pending', 'approved'))"
    )

    # Sering di-update (SPEC: performa jangka panjang), jadi autovacuum dibuat lebih agresif.
    for table in ("leave_balance", "leave_request"):
        op.execute(
            f"ALTER TABLE {table} SET (autovacuum_vacuum_scale_factor = 0.05, "
            "autovacuum_analyze_scale_factor = 0.02)"
        )

    # Konfigurasi HR: hari libur boleh dihapus, tipe cuti dan policy cukup dinonaktifkan/diubah.
    op.execute(grant_statement("leave_type", "SELECT, INSERT, UPDATE"))
    op.execute(grant_statement("leave_policy", "SELECT, INSERT, UPDATE, DELETE"))
    op.execute(grant_statement("holiday_calendar", "SELECT, INSERT, UPDATE, DELETE"))
    op.execute(grant_statement("leave_balance", "SELECT, INSERT, UPDATE"))
    # Pengajuan dan approval tidak pernah dihapus, hanya berubah status.
    op.execute(grant_statement("leave_request", "SELECT, INSERT, UPDATE"))
    op.execute(grant_statement("leave_approval", "SELECT, INSERT, UPDATE"))

    for table in (
        "leave_type",
        "leave_policy",
        "holiday_calendar",
        "leave_balance",
        "leave_request",
        "leave_approval",
    ):
        for statement in rls_statements(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_index("uq_leave_approval_level", table_name="leave_approval")
    op.drop_index("ix_leave_approval_inbox", table_name="leave_approval")
    op.drop_table("leave_approval")
    op.drop_index(
        "uq_leave_request_idempotency",
        table_name="leave_request",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_index("ix_leave_request_tenant_id_period", table_name="leave_request")
    op.drop_index("ix_leave_request_tenant_id_status", table_name="leave_request")
    op.drop_index("ix_leave_request_tenant_id_employee_id_start", table_name="leave_request")
    op.drop_table("leave_request")
    op.drop_index("uq_leave_policy_rule", table_name="leave_policy")
    op.drop_table("leave_policy")
    op.drop_index("uq_leave_balance_key", table_name="leave_balance")
    op.drop_table("leave_balance")
    op.drop_index("uq_leave_type_tenant_id_code", table_name="leave_type")
    op.drop_table("leave_type")
    op.drop_index("uq_holiday_calendar_tenant_id_date", table_name="holiday_calendar")
    op.drop_table("holiday_calendar")
