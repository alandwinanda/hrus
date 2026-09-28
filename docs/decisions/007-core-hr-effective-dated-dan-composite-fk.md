# ADR 007: Core HR effective-dated dan composite FK per tenant

- Status: diterima
- Tanggal: 2026-09-25

## Konteks

Modul pertama (Core HR) menentukan pola untuk semua modul berikutnya:

- Riwayat mutasi, promosi, dan berhenti tidak boleh ditimpa (SPEC: pola JOB PeopleSoft).
- Leave, laporan, dan AI butuh jawaban "jabatan, unit, dan atasan karyawan X per tanggal Y".
- FK biasa (`employee_id -> employee.id`) tidak mengecek tenant. Koneksi owner (CLI, job
  maintenance) melewati RLS, jadi bug di kode admin bisa menautkan data lintas tenant.

## Keputusan

1. **`employee_job` effective-dated dengan `effdt` + `effseq`.** Baris yang berlaku per tanggal X
   adalah `effdt` terbesar yang `<= X`, lalu `effseq` terbesar. Aksi: `hire`, `rehire`, `transfer`,
   `promotion`, `data_change`, `termination`. Status kerja diturunkan dari aksi.
2. **Riwayat append-only.** Role aplikasi hanya punya SELECT dan INSERT di `employee_job`. Koreksi
   di tanggal yang sama memakai `effseq` berikutnya. Baris baru tidak boleh lebih awal dari
   riwayat terakhir (`effdt_out_of_sequence`), supaya riwayat tetap konsisten.
3. **Field yang tidak dikirim diwariskan** dari baris yang berlaku per `effdt` (seperti
   PeopleSoft), jadi mutasi cukup mengirim unit baru.
4. **Jabatan berlaku diambil lewat `LEFT JOIN LATERAL`** dengan index
   `(tenant_id, employee_id, effdt, effseq)`: satu index lookup per karyawan, tanpa denormalisasi.
   Hasil uji di 7.000 karyawan / 24 ribu baris riwayat: list 50 baris < 1 ms, filter unit ~10 ms.
5. **Composite FK `(tenant_id, x_id) -> x(tenant_id, id)`** untuk semua relasi antar tabel tenant,
   dengan `UNIQUE (tenant_id, id)` di tabel yang dirujuk. Database menolak referensi lintas tenant,
   termasuk dari koneksi owner yang melewati RLS.
6. **Visibilitas data karyawan:** HR semua karyawan, karyawan dirinya sendiri, atasan bawahan
   langsung (per jabatan yang berlaku). Selain itu 404, bukan 403, supaya keberadaan data tidak bocor.
7. **Error bisnis standar** (`app/core/errors.py`): `NotFoundError` 404, `ConflictError` 409,
   `RuleViolationError` 422, dengan body `{"detail": {"code", "message"}}`. Hard rules ada di
   `app/rules/` sebagai fungsi murni.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Effective-dated + LATERAL (dipilih) | Riwayat utuh, bisa lihat per tanggal, tanpa job sinkronisasi | Filter per unit memindai banyak karyawan |
| Kolom "jabatan sekarang" di `employee` | Query list paling cepat | Perubahan future-dated butuh job harian untuk sinkron, rawan tidak konsisten |
| Tabel riwayat terpisah + kolom current | Baca cepat, riwayat tetap ada | Dua sumber kebenaran |
| FK biasa + validasi di aplikasi | Skema lebih sederhana | Satu bug di kode admin bisa menautkan data lintas tenant |

## Konsekuensi

- Semua tabel tenant baru wajib `UNIQUE (tenant_id, id)` kalau dirujuk tabel lain, dan FK-nya composite.
- Filter list per unit/atasan memindai karyawan satu per satu. Kalau nanti lambat (misal > 50 ms di
  tenant besar), tambahkan summary table jabatan berlaku yang di-refresh worker (SPEC: summary table).
- Perubahan tanggal masuk (`hire_date`) belum didukung karena terkait baris `hire` pertama.
- Import massal karyawan (Excel) nanti memakai service yang sama lewat job Celery.
