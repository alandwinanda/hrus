"""Data uji performa (SPEC: 7.000 karyawan x 3 tahun). Hanya untuk dev/staging.

Dibuat dengan generate_series di PostgreSQL supaya cepat. Hasilnya deterministik (setseed).
Mencakup Core HR dan Leave (sekitar 10 pengajuan per karyawan per tahun).
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Employee,
    EmployeeJob,
    LeaveApproval,
    LeaveBalance,
    LeavePolicy,
    LeaveRequest,
    LeaveType,
    OrgUnit,
)

DIVISIONS = 10
DEPARTMENTS_PER_DIVISION = 5
MANAGER_POOL = 500  # karyawan nomor 1..500 jadi kandidat atasan


@dataclass(frozen=True, slots=True)
class PerfSeedResult:
    org_units: int
    employees: int
    jobs: int


async def reset_core_hr(session: AsyncSession, tenant_id: UUID) -> None:
    """Hapus data Core HR dan Leave tenant uji. Butuh koneksi owner (role aplikasi tidak punya
    DELETE)."""
    for model in (LeaveApproval, LeaveRequest, LeaveBalance, LeavePolicy, LeaveType):
        await session.execute(delete(model).where(model.tenant_id == tenant_id))
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


@dataclass(frozen=True, slots=True)
class LeaveSeedResult:
    requests: int
    approvals: int
    balances: int


LEAVE_SLOTS_PER_YEAR = 10  # jarak antar slot 35 hari, jadi pengajuan satu karyawan tidak overlap


async def seed_leave(session: AsyncSession, tenant_id: UUID, *, years: int = 3) -> LeaveSeedResult:
    """Pengajuan cuti tahun lalu sampai tahun berjalan: yang sudah lewat sebagian besar disetujui,
    yang akan datang masih menunggu. Saldo tahunan disesuaikan dengan pengajuan yang ada."""
    params = {"t": tenant_id, "years": years, "slots": LEAVE_SLOTS_PER_YEAR}
    await session.execute(text("SELECT setseed(0.24)"))
    await session.execute(
        text(
            """
            INSERT INTO leave_type (tenant_id, code, name, requires_balance, is_paid,
                                    min_notice_days, allow_backdated, approval_levels, is_active)
            VALUES (:t, 'CUTI_TAHUNAN', 'Cuti Tahunan', true, true, 3, false, 1, true),
                   (:t, 'SAKIT', 'Sakit', false, true, 0, true, 1, true)
            """
        ),
        params,
    )
    await session.execute(
        text(
            """
            INSERT INTO leave_policy (tenant_id, leave_type_id, min_service_months, annual_days,
                                      max_carry_over_days, carry_over_expiry_months)
            SELECT :t, id, 12, 12, 6, 3 FROM leave_type WHERE tenant_id = :t
              AND code = 'CUTI_TAHUNAN'
            """
        ),
        params,
    )

    # Slot k di tahun y mulai hari ke k*35 + acak(0..20), digeser ke hari kerja, 1-3 hari.
    await session.execute(
        text(
            """
            WITH types AS (
                SELECT (SELECT id FROM leave_type WHERE tenant_id = :t AND code = 'CUTI_TAHUNAN')
                           AS annual,
                       (SELECT id FROM leave_type WHERE tenant_id = :t AND code = 'SAKIT') AS sick
            ),
            slots AS (
                SELECT e.id AS employee_id, e.hire_date,
                       make_date(y, 1, 1) + k * 35 + (random() * 20)::int AS raw_start,
                       (random() * 2)::int AS extra_days,
                       random() AS r_type, random() AS r_status
                FROM employee e
                CROSS JOIN generate_series(
                    extract(year FROM current_date)::int - :years + 1,
                    extract(year FROM current_date)::int
                ) y
                CROSS JOIN generate_series(0, :slots - 1) k
                WHERE e.tenant_id = :t
            ),
            shaped AS (
                SELECT s.*,
                       s.raw_start + CASE extract(isodow FROM s.raw_start)
                                         WHEN 6 THEN 2 WHEN 7 THEN 1 ELSE 0 END AS start_date
                FROM slots s
            ),
            ranged AS (
                SELECT sh.*, sh.start_date + sh.extra_days AS end_date,
                       (SELECT count(*) FROM generate_series(sh.start_date,
                                                             sh.start_date + sh.extra_days,
                                                             interval '1 day') d
                        WHERE extract(isodow FROM d) < 6) AS days
                FROM shaped sh
                WHERE sh.start_date >= sh.hire_date
                  AND sh.start_date <= current_date + 90
            )
            INSERT INTO leave_request (
                tenant_id, employee_id, leave_type_id, start_date, end_date, days, status,
                approval_levels, current_level, requested_by_user_id, validation,
                decided_at, cancelled_at, created_at, updated_at
            )
            SELECT :t, r.employee_id,
                   CASE WHEN r.r_type < 0.45 THEN types.annual ELSE types.sick END,
                   r.start_date, r.end_date, r.days,
                   CASE WHEN r.start_date > current_date THEN 'pending'
                        WHEN r.r_status < 0.85 THEN 'approved'
                        WHEN r.r_status < 0.92 THEN 'rejected'
                        ELSE 'cancelled' END,
                   1, 1, gen_random_uuid(), '{}'::jsonb,
                   CASE WHEN r.start_date <= current_date AND r.r_status < 0.92
                        THEN r.start_date - 3 END,
                   CASE WHEN r.start_date <= current_date AND r.r_status >= 0.92
                        THEN r.start_date - 2 END,
                   r.start_date - 7, r.start_date - 7
            FROM ranged r CROSS JOIN types
            ORDER BY r.start_date  -- urutan fisik seperti data asli (dibuat seiring waktu)
            """
        ),
        params,
    )

    # Satu level approval: atasan dari jabatan yang berlaku di tanggal mulai (NULL = HR).
    await session.execute(
        text(
            """
            INSERT INTO leave_approval (tenant_id, leave_request_id, level, approver_employee_id,
                                        status, decided_at, created_at)
            SELECT :t, lr.id, 1, job.supervisor_employee_id,
                   CASE lr.status WHEN 'cancelled' THEN 'skipped' ELSE lr.status END,
                   lr.decided_at, lr.created_at
            FROM leave_request lr
            LEFT JOIN LATERAL (
                SELECT j.supervisor_employee_id FROM employee_job j
                WHERE j.tenant_id = lr.tenant_id AND j.employee_id = lr.employee_id
                  AND j.effdt <= lr.start_date
                ORDER BY j.effdt DESC, j.effseq DESC LIMIT 1
            ) job ON true
            WHERE lr.tenant_id = :t
            """
        ),
        params,
    )

    # Saldo tahunan = jatah policy, ditambah koreksi kalau pengajuan acak melebihi jatah.
    await session.execute(
        text(
            """
            INSERT INTO leave_balance (tenant_id, employee_id, leave_type_id, year, entitled,
                                       carried_over, adjusted, used, pending)
            SELECT :t, lr.employee_id, lr.leave_type_id, extract(year FROM lr.start_date)::int,
                   12, 0,
                   greatest(0, sum(lr.days) FILTER (WHERE lr.status IN ('approved', 'pending'))
                               - 12),
                   coalesce(sum(lr.days) FILTER (WHERE lr.status = 'approved'), 0),
                   coalesce(sum(lr.days) FILTER (WHERE lr.status = 'pending'), 0)
            FROM leave_request lr
            JOIN leave_type lt ON lt.tenant_id = lr.tenant_id AND lt.id = lr.leave_type_id
            WHERE lr.tenant_id = :t AND lt.requires_balance
            GROUP BY lr.employee_id, lr.leave_type_id, extract(year FROM lr.start_date)
            """
        ),
        params,
    )

    for table in ("leave_type", "leave_policy", "leave_request", "leave_approval", "leave_balance"):
        await session.execute(text(f"ANALYZE {table}"))

    async def count(
        model: type[LeaveRequest] | type[LeaveApproval] | type[LeaveBalance],
    ) -> int:
        return int(
            await session.scalar(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            )
            or 0
        )

    return LeaveSeedResult(
        requests=await count(LeaveRequest),
        approvals=await count(LeaveApproval),
        balances=await count(LeaveBalance),
    )
