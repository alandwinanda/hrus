"""Hard rules Core HR. Fungsi murni: tidak menyentuh database, mudah dites, dan dipakai ulang
oleh endpoint validate, import massal, dan AI (lewat API yang sama).
"""

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from app.core.errors import RuleViolationError
from app.models import EmploymentStatus, JobAction


def employment_status_for(action: JobAction) -> EmploymentStatus:
    if action == JobAction.TERMINATION:
        return EmploymentStatus.TERMINATED
    return EmploymentStatus.ACTIVE


def check_new_job_date(
    *, action: JobAction, effdt: date, hire_date: date, latest_effdt: date | None
) -> None:
    if action == JobAction.HIRE:
        raise RuleViolationError(
            "Aksi 'hire' hanya dibuat otomatis saat data karyawan dibuat.",
            code="hire_only_on_create",
        )
    if effdt < hire_date:
        raise RuleViolationError(
            "Tanggal efektif tidak boleh sebelum tanggal masuk karyawan.",
            code="effdt_before_hire",
        )
    if latest_effdt is not None and effdt < latest_effdt:
        raise RuleViolationError(
            f"Sudah ada riwayat jabatan per {latest_effdt.isoformat()}. "
            "Tanggal efektif baru tidak boleh lebih awal dari riwayat terakhir.",
            code="effdt_out_of_sequence",
        )


def check_action_transition(*, action: JobAction, status_before: EmploymentStatus) -> None:
    terminated = status_before == EmploymentStatus.TERMINATED
    if action == JobAction.REHIRE and not terminated:
        raise RuleViolationError(
            "Rehire hanya untuk karyawan yang sudah berhenti.",
            code="rehire_requires_termination",
        )
    if action != JobAction.REHIRE and terminated:
        raise RuleViolationError(
            "Karyawan sudah berhenti. Gunakan aksi 'rehire' untuk mempekerjakan kembali.",
            code="employee_terminated",
        )


def check_supervisor(*, employee_id: UUID | None, supervisor_id: UUID | None) -> None:
    if supervisor_id is not None and supervisor_id == employee_id:
        raise RuleViolationError(
            "Karyawan tidak bisa menjadi atasan dirinya sendiri.", code="own_supervisor"
        )


def check_org_parent(*, unit_id: UUID, parent_ancestors: Iterable[UUID]) -> None:
    """parent_ancestors = parent baru beserta semua leluhurnya."""
    if unit_id in set(parent_ancestors):
        raise RuleViolationError(
            "Parent tidak boleh unit itu sendiri atau salah satu sub-unitnya.",
            code="org_unit_cycle",
        )
