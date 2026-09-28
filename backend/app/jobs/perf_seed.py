"""Data uji performa (SPEC: 7.000 karyawan x 3 tahun). Hanya untuk dev/staging.

Dibuat dengan generate_series di PostgreSQL supaya cepat. Hasilnya deterministik (setseed).
Saat ini mencakup Core HR. Data cuti ditambahkan saat modul Leave dibuat.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Employee, EmployeeJob, OrgUnit

DIVISIONS = 10
DEPARTMENTS_PER_DIVISION = 5
MANAGER_POOL = 500  # karyawan nomor 1..500 jadi kandidat atasan


@dataclass(frozen=True, slots=True)
class PerfSeedResult:
    org_units: int
    employees: int
    jobs: int


async def reset_core_hr(session: AsyncSession, tenant_id: UUID) -> None:
    """Hapus data Core HR tenant uji. Butuh koneksi owner (role aplikasi tidak punya DELETE)."""
    await session.execute(delete(EmployeeJob).where(EmployeeJob.tenant_id == tenant_id))
    await session.execute(
        OrgUnit.__table__.update()
        .where(OrgUnit.tenant_id == tenant_id)
        .values(manager_employee_id=None, parent_id=None)
    )
    await session.execute(delete(Employee).where(Employee.tenant_id == tenant_id))
    await session.execute(delete(OrgUnit).where(OrgUnit.tenant_id == tenant_id))


async def seed_core_hr(
    session: AsyncSession, tenant_id: UUID, *, employees: int = 7000, years: int = 3
) -> PerfSeedResult:
    params = {"t": tenant_id, "n": employees, "days": years * 365}
    await session.execute(text("SELECT setseed(0.42)"))

    # Struktur: 1 root, 10 divisi, 5 departemen per divisi.
    await session.execute(
        text("INSERT INTO org_unit (tenant_id, code, name) VALUES (:t, 'ROOT', 'Direksi')"),
        params,
    )
    await session.execute(
        text(
            """
            INSERT INTO org_unit (tenant_id, code, name, parent_id)
            SELECT :t, 'DIV' || d, 'Divisi ' || d,
                   (SELECT id FROM org_unit WHERE tenant_id = :t AND code = 'ROOT')
            FROM generate_series(1, :divisions) d
            """
        ),
        params | {"divisions": DIVISIONS},
    )
    await session.execute(
        text(
            """
            INSERT INTO org_unit (tenant_id, code, name, parent_id)
            SELECT :t, 'DEP' || d || '-' || p, 'Departemen ' || d || '-' || p, div.id
            FROM generate_series(1, :divisions) d
            CROSS JOIN generate_series(1, :per_division) p
            JOIN org_unit div ON div.tenant_id = :t AND div.code = 'DIV' || d
            """
        ),
        params | {"divisions": DIVISIONS, "per_division": DEPARTMENTS_PER_DIVISION},
    )

    # Karyawan dengan tanggal masuk tersebar selama `years` tahun terakhir.
    await session.execute(
        text(
            """
            INSERT INTO employee (tenant_id, employee_number, full_name, work_email, hire_date)
            SELECT :t, 'P' || lpad(g::text, 6, '0'), 'Karyawan ' || g,
                   'karyawan' || g || '@perf.test',
                   current_date - (random() * :days)::int
            FROM generate_series(1, :n) g
            """
        ),
        params,
    )

    # Baris hire untuk semua karyawan, lalu perubahan tiap ~6 bulan sampai hari ini.
    await session.execute(
        text(
            """
            WITH dept AS (
                SELECT array_agg(id ORDER BY code) AS ids
                FROM org_unit WHERE tenant_id = :t AND code LIKE 'DEP%'
            ),
            managers AS (
                SELECT array_agg(id ORDER BY employee_number) AS ids
                FROM employee WHERE tenant_id = :t
                  AND employee_number <= 'P' || lpad(CAST(:pool AS text), 6, '0')
            ),
            changes AS (
                SELECT e.id AS employee_id, 0 AS step, e.hire_date AS effdt
                FROM employee e WHERE e.tenant_id = :t
                UNION ALL
                SELECT e.id, k, e.hire_date + k * 180 + (random() * 29)::int
                FROM employee e CROSS JOIN generate_series(1, 6) k
                WHERE e.tenant_id = :t AND e.hire_date + k * 180 + 29 <= current_date
            )
            INSERT INTO employee_job (
                tenant_id, employee_id, effdt, effseq, action, job_title, grade,
                org_unit_id, supervisor_employee_id, employment_type, employment_status
            )
            SELECT :t, c.employee_id, c.effdt, 0,
                   CASE WHEN c.step = 0 THEN 'hire'
                        WHEN c.step % 2 = 1 THEN 'transfer' ELSE 'promotion' END,
                   CASE WHEN c.step >= 2 THEN 'Senior Staff' ELSE 'Staff' END,
                   'G' || (1 + c.step / 2),
                   dept.ids[1 + floor(random() * array_length(dept.ids, 1))::int],
                   NULLIF(managers.ids[1 + floor(random() * array_length(managers.ids, 1))::int],
                          c.employee_id),
                   CASE WHEN random() < 0.8 THEN 'permanent' ELSE 'contract' END,
                   'active'
            FROM changes c CROSS JOIN dept CROSS JOIN managers
            """
        ),
        params | {"pool": MANAGER_POOL},
    )

    # Sekitar 5% karyawan berhenti 30 hari setelah perubahan terakhirnya.
    await session.execute(
        text(
            """
            INSERT INTO employee_job (
                tenant_id, employee_id, effdt, effseq, action, job_title, grade,
                org_unit_id, supervisor_employee_id, employment_type, employment_status
            )
            SELECT DISTINCT ON (j.employee_id)
                   :t, j.employee_id, j.effdt + 30, 0, 'termination', j.job_title, j.grade,
                   j.org_unit_id, j.supervisor_employee_id, j.employment_type, 'terminated'
            FROM employee_job j
            WHERE j.tenant_id = :t AND j.effdt + 30 <= current_date
              AND abs(hashtext(j.employee_id::text)) % 20 = 0
            ORDER BY j.employee_id, j.effdt DESC, j.effseq DESC
            """
        ),
        params,
    )

    for table in ("org_unit", "employee", "employee_job"):
        await session.execute(text(f"ANALYZE {table}"))

    async def count(model: type[OrgUnit] | type[Employee] | type[EmployeeJob]) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            )
            or 0
        )

    return PerfSeedResult(
        org_units=await count(OrgUnit),
        employees=await count(Employee),
        jobs=await count(EmployeeJob),
    )
