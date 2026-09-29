"""Katalog dataset laporan: view, kolom yang boleh dipilih, label, tipe, dan deskripsi bisnis.

Deskripsi kolom juga menjadi data dictionary untuk Reporting Agent nanti. Setiap kolom di sini
wajib ada di view-nya (dicek test). Kolom sensitif (NIK KTP, gaji, rekening) tidak pernah masuk.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.core.errors import NotFoundError, RuleViolationError
from app.models import EmploymentType, LeaveRequestStatus


class ColumnType(StrEnum):
    TEXT = "text"
    ENUM = "enum"
    NUMBER = "number"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"


@dataclass(frozen=True, slots=True)
class ColumnDef:
    key: str
    label: str
    type: ColumnType
    description: str
    enum_values: tuple[tuple[str, str], ...] = ()  # (nilai, label)

    def enum_label(self, value: object) -> str:
        return dict(self.enum_values).get(str(value), str(value))


@dataclass(frozen=True, slots=True)
class Dataset:
    key: str
    view: str
    label: str
    description: str
    columns: tuple[ColumnDef, ...]
    # Dataset transaksi: tanpa filter di kolom tanggal ini, otomatis dibatasi tahun berjalan.
    default_period_column: str | None = None

    def column(self, key: str) -> ColumnDef:
        for column in self.columns:
            if column.key == key:
                return column
        raise RuleViolationError(
            f"Kolom '{key}' tidak ada di dataset {self.label}.", code="report_unknown_column"
        )


EMPLOYEE_STATUS = (("active", "Aktif"), ("terminated", "Berhenti"), ("pre_hire", "Belum mulai"))
EMPLOYMENT_TYPE_LABELS = {
    EmploymentType.PERMANENT: "Tetap (PKWTT)",
    EmploymentType.CONTRACT: "Kontrak (PKWT)",
    EmploymentType.INTERN: "Magang",
    EmploymentType.DAILY: "Harian",
}
EMPLOYMENT_TYPES = tuple((t.value, EMPLOYMENT_TYPE_LABELS.get(t, t.value)) for t in EmploymentType)
LEAVE_STATUS = (
    (LeaveRequestStatus.PENDING.value, "Menunggu"),
    (LeaveRequestStatus.APPROVED.value, "Disetujui"),
    (LeaveRequestStatus.REJECTED.value, "Ditolak"),
    (LeaveRequestStatus.CANCELLED.value, "Dibatalkan"),
)

T = ColumnType


def _employee_columns(status_key: str | None = "status") -> tuple[ColumnDef, ...]:
    return (
        ColumnDef("employee_number", "Nomor karyawan", T.TEXT, "Nomor induk karyawan internal."),
        ColumnDef("full_name", "Nama", T.TEXT, "Nama lengkap karyawan."),
        ColumnDef("org_unit_code", "Kode unit", T.TEXT, "Kode unit organisasi saat ini."),
        ColumnDef("org_unit_name", "Unit", T.TEXT, "Nama unit organisasi saat ini."),
    ) + (
        (
            ColumnDef(
                status_key,
                "Status karyawan",
                T.ENUM,
                "Status kerja per hari ini: aktif, berhenti, atau belum mulai bekerja.",
                EMPLOYEE_STATUS,
            ),
        )
        if status_key
        else ()
    )


EMPLOYEES = Dataset(
    key="employees",
    view="v_employee",
    label="Karyawan",
    description="Satu baris per karyawan dengan jabatan yang berlaku hari ini.",
    columns=(
        *_employee_columns(),
        ColumnDef("work_email", "Email kantor", T.TEXT, "Alamat email kantor."),
        ColumnDef("hire_date", "Tanggal masuk", T.DATE, "Tanggal mulai bekerja."),
        ColumnDef(
            "service_months",
            "Masa kerja (bulan)",
            T.NUMBER,
            "Masa kerja penuh dalam bulan per hari ini.",
        ),
        ColumnDef("job_title", "Jabatan", T.TEXT, "Nama jabatan yang berlaku hari ini."),
        ColumnDef("grade", "Grade", T.TEXT, "Grade jabatan yang berlaku hari ini."),
        ColumnDef(
            "employment_type",
            "Jenis kepegawaian",
            T.ENUM,
            "Tetap (PKWTT), kontrak (PKWT), magang, atau harian.",
            EMPLOYMENT_TYPES,
        ),
        ColumnDef("supervisor_number", "Nomor atasan", T.TEXT, "Nomor karyawan atasan langsung."),
        ColumnDef("supervisor_name", "Atasan", T.TEXT, "Nama atasan langsung."),
        ColumnDef(
            "job_effective_date",
            "Berlaku sejak",
            T.DATE,
            "Tanggal efektif jabatan yang berlaku hari ini (mutasi/promosi terakhir).",
        ),
    ),
)

ORG_UNITS = Dataset(
    key="org_units",
    view="v_org_unit",
    label="Unit organisasi",
    description="Satu baris per unit organisasi, termasuk jumlah karyawan aktif.",
    columns=(
        ColumnDef("code", "Kode unit", T.TEXT, "Kode unik unit organisasi."),
        ColumnDef("name", "Nama unit", T.TEXT, "Nama unit organisasi."),
        ColumnDef("is_active", "Aktif", T.BOOLEAN, "Unit masih dipakai atau sudah ditutup."),
        ColumnDef("parent_code", "Kode unit induk", T.TEXT, "Kode unit satu tingkat di atasnya."),
        ColumnDef("parent_name", "Unit induk", T.TEXT, "Nama unit satu tingkat di atasnya."),
        ColumnDef("manager_number", "Nomor kepala unit", T.TEXT, "Nomor karyawan kepala unit."),
        ColumnDef("manager_name", "Kepala unit", T.TEXT, "Nama kepala unit."),
        ColumnDef(
            "active_headcount",
            "Karyawan aktif",
            T.NUMBER,
            "Jumlah karyawan aktif yang jabatannya saat ini di unit ini (tanpa sub-unit).",
        ),
    ),
)

LEAVE_BALANCES = Dataset(
    key="leave_balances",
    view="v_leave_balance",
    label="Saldo cuti",
    description="Satu baris per karyawan, tipe cuti, dan tahun. Angka saldo dalam hari kerja.",
    columns=(
        *_employee_columns("employee_status"),
        ColumnDef("year", "Tahun", T.NUMBER, "Tahun saldo."),
        ColumnDef("leave_type_code", "Kode tipe cuti", T.TEXT, "Kode tipe cuti."),
        ColumnDef("leave_type_name", "Tipe cuti", T.TEXT, "Nama tipe cuti."),
        ColumnDef("entitled", "Jatah", T.NUMBER, "Jatah tahun ini dari policy."),
        ColumnDef("carried_over", "Carry-over", T.NUMBER, "Sisa tahun lalu yang dibawa."),
        ColumnDef("adjusted", "Koreksi", T.NUMBER, "Koreksi manual HR (bisa negatif)."),
        ColumnDef("used", "Terpakai", T.NUMBER, "Hari cuti yang sudah disetujui."),
        ColumnDef("pending", "Diajukan", T.NUMBER, "Hari cuti yang masih menunggu persetujuan."),
        ColumnDef("expired", "Hangus", T.NUMBER, "Carry-over yang hangus karena tidak dipakai."),
        ColumnDef(
            "available",
            "Sisa",
            T.NUMBER,
            "Jatah + carry-over + koreksi - terpakai - diajukan - hangus.",
        ),
        ColumnDef(
            "carry_over_expires_on",
            "Carry-over hangus pada",
            T.DATE,
            "Tanggal carry-over yang belum dipakai mulai hangus.",
        ),
    ),
)

LEAVE_REQUESTS = Dataset(
    key="leave_requests",
    view="v_leave_request",
    label="Pengajuan cuti",
    description=(
        "Satu baris per pengajuan cuti. Tanpa filter tanggal mulai, dibatasi tahun berjalan."
    ),
    default_period_column="start_date",
    columns=(
        *_employee_columns(None),
        ColumnDef("leave_type_code", "Kode tipe cuti", T.TEXT, "Kode tipe cuti."),
        ColumnDef("leave_type_name", "Tipe cuti", T.TEXT, "Nama tipe cuti."),
        ColumnDef("start_date", "Mulai", T.DATE, "Tanggal mulai cuti."),
        ColumnDef("end_date", "Selesai", T.DATE, "Tanggal selesai cuti (inklusif)."),
        ColumnDef(
            "days",
            "Jumlah hari",
            T.NUMBER,
            "Hari kerja yang dipotong, tanpa akhir pekan dan libur.",
        ),
        ColumnDef("status", "Status", T.ENUM, "Status pengajuan.", LEAVE_STATUS),
        ColumnDef("reason", "Alasan", T.TEXT, "Alasan yang ditulis karyawan (teks bebas)."),
        ColumnDef("created_at", "Diajukan pada", T.DATETIME, "Waktu pengajuan dibuat."),
        ColumnDef(
            "decided_at",
            "Diputuskan pada",
            T.DATETIME,
            "Waktu keputusan akhir (disetujui/ditolak).",
        ),
        ColumnDef("cancelled_at", "Dibatalkan pada", T.DATETIME, "Waktu pengajuan dibatalkan."),
    ),
)

DATASETS: dict[str, Dataset] = {
    dataset.key: dataset for dataset in (EMPLOYEES, ORG_UNITS, LEAVE_BALANCES, LEAVE_REQUESTS)
}


def get_dataset(key: str) -> Dataset:
    dataset = DATASETS.get(key)
    if dataset is None:
        raise NotFoundError(f"Dataset laporan '{key}' tidak ada.", code="report_unknown_dataset")
    return dataset
