"""Hard rules cuti. Fungsi murni: dipakai endpoint validate (dry-run), submit, dan nanti AI
lewat API yang sama, jadi hasil validasi selalu konsisten di form dan chat.

Setiap cek mengembalikan Violation atau None, supaya validate bisa menampilkan semua error
sekaligus, sedangkan submit menolak di error pertama.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol
from uuid import UUID

from app.core.errors import RuleViolationError
from app.models import EmploymentStatus, LeaveRequestStatus
from app.models.leave import MAX_REQUEST_SPAN_DAYS


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    message: str


class PolicyLike(Protocol):
    leave_type_id: UUID
    grade: str | None
    min_service_months: int
    annual_days: int
    max_carry_over_days: int
    carry_over_expiry_months: int


def service_months(hire_date: date, as_of: date) -> int:
    """Masa kerja dalam bulan penuh per tanggal as_of."""
    months = (as_of.year - hire_date.year) * 12 + (as_of.month - hire_date.month)
    if as_of.day < hire_date.day:
        months -= 1
    return max(months, 0)


def pick_policy[P: PolicyLike](
    policies: Iterable[P], *, leave_type_id: UUID, grade: str | None, months: int
) -> P | None:
    """Policy paling spesifik: grade yang cocok dulu (lalu policy umum), masa kerja minimum
    tertinggi yang sudah terpenuhi."""
    grade = grade.upper() if grade else None
    candidates = [
        p
        for p in policies
        if p.leave_type_id == leave_type_id
        and p.min_service_months <= months
        and (p.grade is None or p.grade == grade)
    ]
    return min(candidates, key=lambda p: (p.grade is None, -p.min_service_months), default=None)


def carry_over_expiry(year: int, expiry_months: int) -> date | None:
    """Tanggal carry-over dari tahun sebelumnya mulai hangus. 0 bulan = tidak hangus."""
    if expiry_months <= 0:
        return None
    return date(year + expiry_months // 12, expiry_months % 12 + 1, 1)


def working_days(start: date, end: date, holidays: set[date]) -> int:
    """Hari kerja Senin-Jumat di antara start dan end (inklusif), dikurangi hari libur."""
    days = 0
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in holidays:
            days += 1
        current += timedelta(days=1)
    return days


def check_range(start: date, end: date) -> Violation | None:
    if end < start:
        return Violation("invalid_date_range", "Tanggal selesai tidak boleh sebelum tanggal mulai.")
    if start.year != end.year:
        return Violation(
            "cross_year",
            "Pengajuan tidak boleh melewati pergantian tahun. Pecah jadi dua pengajuan.",
        )
    if (end - start).days + 1 > MAX_REQUEST_SPAN_DAYS:
        return Violation(
            "range_too_long", f"Rentang cuti maksimal {MAX_REQUEST_SPAN_DAYS} hari kalender."
        )
    return None


def check_working_days(days: int) -> Violation | None:
    if days <= 0:
        return Violation("no_working_days", "Rentang tanggal tidak berisi hari kerja.")
    return None


def check_notice(
    *, start: date, today: date, min_notice_days: int, allow_backdated: bool
) -> Violation | None:
    if start < today and not allow_backdated:
        return Violation("start_in_past", "Tipe cuti ini tidak bisa diajukan untuk tanggal lampau.")
    if start >= today and (start - today).days < min_notice_days:
        return Violation(
            "insufficient_notice",
            f"Tipe cuti ini harus diajukan minimal {min_notice_days} hari sebelumnya.",
        )
    return None


def check_max_days(days: int, max_days: int | None) -> Violation | None:
    if max_days is not None and days > max_days:
        return Violation("exceeds_max_days", f"Maksimal {max_days} hari kerja per pengajuan.")
    return None


def check_balance(*, available: int, days: int) -> Violation | None:
    if days > available:
        return Violation(
            "insufficient_balance",
            f"Saldo cuti tidak cukup: tersedia {available} hari, diajukan {days} hari.",
        )
    return None


def check_employee_active(status: EmploymentStatus | None) -> Violation | None:
    if status != EmploymentStatus.ACTIVE:
        return Violation("employee_not_active", "Karyawan tidak aktif pada tanggal mulai cuti.")
    return None


def check_no_overlap(has_overlap: bool) -> Violation | None:
    if has_overlap:
        return Violation(
            "overlapping_request",
            "Sudah ada pengajuan cuti lain (menunggu atau disetujui) di tanggal yang sama.",
        )
    return None


def ensure_can_cancel(*, status: LeaveRequestStatus, start: date, today: date) -> None:
    if status == LeaveRequestStatus.PENDING:
        return
    if status == LeaveRequestStatus.APPROVED and start > today:
        return
    raise RuleViolationError(
        "Hanya pengajuan yang masih menunggu, atau disetujui dan belum dimulai, "
        "yang bisa dibatalkan.",
        code="cannot_cancel",
    )


def ensure_pending(status: LeaveRequestStatus) -> None:
    if status != LeaveRequestStatus.PENDING:
        raise RuleViolationError(
            "Pengajuan ini sudah diputuskan atau dibatalkan.", code="request_not_pending"
        )
