"""core hr org unit employee job

Revision ID: bb316c7a18d7
Revises: a9547810788a
Create Date: 2026-09-25 10:26:24.669393

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.tenant import grant_statement, rls_statements

# revision identifiers, used by Alembic.
revision: str = "bb316c7a18d7"
down_revision: str | Sequence[str] | None = "a9547810788a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "employee",
        sa.Column("employee_number", sa.String(length=32), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("work_email", sa.String(length=254), nullable=True),
        sa.Column("hire_date", sa.Date(), nullable=False),
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
            "work_email = lower(work_email)", name=op.f("ck_employee_work_email_lowercase")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_employee_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_employee_tenant_id_id"),
    )
    op.create_index(
        "uq_employee_tenant_id_employee_number",
        "employee",
        ["tenant_id", "employee_number"],
        unique=True,
    )
    op.create_index(
        "uq_employee_tenant_id_work_email",
        "employee",
        ["tenant_id", "work_email"],
        unique=True,
        postgresql_where=sa.text("work_email IS NOT NULL"),
    )
    op.create_table(
        "org_unit",
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("manager_employee_id", sa.Uuid(), nullable=True),
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
            "parent_id IS NULL OR parent_id <> id", name=op.f("ck_org_unit_not_own_parent")
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "manager_employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_org_unit_manager",
            use_alter=True,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "parent_id"],
            ["org_unit.tenant_id", "org_unit.id"],
            name="fk_org_unit_parent",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_org_unit_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_org_unit")),
        sa.UniqueConstraint("tenant_id", "id", name="uq_org_unit_tenant_id_id"),
    )
    op.create_index(
        "ix_org_unit_tenant_id_parent_id", "org_unit", ["tenant_id", "parent_id"], unique=False
    )
    op.create_index("uq_org_unit_tenant_id_code", "org_unit", ["tenant_id", "code"], unique=True)
    op.create_table(
        "employee_job",
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("effdt", sa.Date(), nullable=False),
        sa.Column("effseq", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column("job_title", sa.String(length=120), nullable=False),
        sa.Column("grade", sa.String(length=20), nullable=False),
        sa.Column("org_unit_id", sa.Uuid(), nullable=False),
        sa.Column("supervisor_employee_id", sa.Uuid(), nullable=True),
        sa.Column("employment_type", sa.String(length=20), nullable=False),
        sa.Column("employment_status", sa.String(length=20), nullable=False),
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
            "action IN ('hire', 'rehire', 'transfer', 'promotion', 'data_change', 'termination')",
            name=op.f("ck_employee_job_action_valid"),
        ),
        sa.CheckConstraint(
            "employment_status IN ('active', 'terminated')",
            name=op.f("ck_employee_job_status_valid"),
        ),
        sa.CheckConstraint(
            "employment_type IN ('permanent', 'contract', 'intern', 'daily')",
            name=op.f("ck_employee_job_type_valid"),
        ),
        sa.CheckConstraint("effseq >= 0", name=op.f("ck_employee_job_effseq_positive")),
        sa.CheckConstraint(
            "supervisor_employee_id IS NULL OR supervisor_employee_id <> employee_id",
            name=op.f("ck_employee_job_not_own_supervisor"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_employee_job_employee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "org_unit_id"],
            ["org_unit.tenant_id", "org_unit.id"],
            name="fk_employee_job_org_unit",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supervisor_employee_id"],
            ["employee.tenant_id", "employee.id"],
            name="fk_employee_job_supervisor",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenant.id"],
            name=op.f("fk_employee_job_tenant_id_tenant"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_employee_job")),
    )
    op.create_index(
        "ix_employee_job_tenant_id_org_unit_id",
        "employee_job",
        ["tenant_id", "org_unit_id"],
        unique=False,
    )
    op.create_index(
        "ix_employee_job_tenant_id_supervisor",
        "employee_job",
        ["tenant_id", "supervisor_employee_id"],
        unique=False,
    )
    op.create_index(
        "uq_employee_job_effective",
        "employee_job",
        ["tenant_id", "employee_id", "effdt", "effseq"],
        unique=True,
    )
    op.create_index(
        "uq_app_user_tenant_id_employee_id",
        "app_user",
        ["tenant_id", "employee_id"],
        unique=True,
        postgresql_where=sa.text("employee_id IS NOT NULL"),
    )
    op.create_foreign_key(
        "fk_app_user_employee",
        "app_user",
        "employee",
        ["tenant_id", "employee_id"],
        ["tenant_id", "id"],
    )

    # FK dengan use_alter tidak ikut dibuat oleh create_table, jadi dibuat eksplisit di sini.
    op.create_foreign_key(
        "fk_org_unit_manager",
        "org_unit",
        "employee",
        ["tenant_id", "manager_employee_id"],
        ["tenant_id", "id"],
    )

    # Data master cukup dinonaktifkan, tidak dihapus (tanpa DELETE).
    op.execute(grant_statement("org_unit", "SELECT, INSERT, UPDATE"))
    op.execute(grant_statement("employee", "SELECT, INSERT, UPDATE"))
    # Riwayat jabatan append-only: koreksi = baris baru dengan effseq berikutnya.
    op.execute(grant_statement("employee_job", "SELECT, INSERT"))

    for table in ("org_unit", "employee", "employee_job"):
        for statement in rls_statements(table):
            op.execute(statement)


def downgrade() -> None:
    op.drop_constraint("fk_app_user_employee", "app_user", type_="foreignkey")
    op.drop_index(
        "uq_app_user_tenant_id_employee_id",
        table_name="app_user",
        postgresql_where=sa.text("employee_id IS NOT NULL"),
    )
    op.drop_index("uq_employee_job_effective", table_name="employee_job")
    op.drop_index("ix_employee_job_tenant_id_supervisor", table_name="employee_job")
    op.drop_index("ix_employee_job_tenant_id_org_unit_id", table_name="employee_job")
    op.drop_table("employee_job")
    op.drop_index("uq_org_unit_tenant_id_code", table_name="org_unit")
    op.drop_index("ix_org_unit_tenant_id_parent_id", table_name="org_unit")
    op.drop_table("org_unit")
    op.drop_index(
        "uq_employee_tenant_id_work_email",
        table_name="employee",
        postgresql_where=sa.text("work_email IS NOT NULL"),
    )
    op.drop_index("uq_employee_tenant_id_employee_number", table_name="employee")
    op.drop_table("employee")
