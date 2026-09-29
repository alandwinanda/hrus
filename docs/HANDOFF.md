# Serah terima: lanjut dari sesi cloud ke Claude Code lokal

Dokumen ini untuk sesi Claude Code berikutnya (lokal). Baca bersama `CLAUDE.md` (aturan kerja),
`docs/SPEC.md` (acuan utama), dan `docs/decisions/` (ADR). Hapus atau perbarui file ini setelah
pekerjaan di bawah selesai.

Branch kerja: `claude/gifted-ride-z1l8v2` (belum di-merge ke `main`, belum ada PR).

## Setup lokal (sekali)

Butuh Docker + Compose v2, `uv`, Node 22, `make`, git. Di Windows jalankan dari WSL2.

```
git clone https://github.com/alandwinanda/hrus.git && cd hrus
git checkout claude/gifted-ride-z1l8v2
make install      # uv sync per service, npm ci, pre-commit
make up           # buat .env dari .env.example, http://localhost:8080
make migrate
make seed-dev     # hr@ / atasan@ / karyawan@demo.test, password demo-password
make test && make lint
```

Database test `hrus_test` dibuat `infra/postgres/init.sql` saat volume postgres baru. Untuk
mencoba AI, isi `AI_SECRET_KEY` di `.env` (cara membuat ada di `.env.example`).

## Sudah selesai (CI hijau)

| Area | Isi | ADR |
| --- | --- | --- |
| Fondasi | Monorepo, Docker Compose, CI, hook Claude Code, skill `/new-module` | 001–005 |
| Core API | Auth JWT + refresh cookie, role, tenant, RLS fail-closed, audit_log | 006 |
| Core HR | org_unit, employee, employee_job effective-dated, composite FK | 007 |
| Leave | Tipe, policy, libur, saldo tersimpan, pengajuan, approval 1–2 level, kalender | 008 |
| Job | job_run/chunk/log/schedule, runner per chunk, scheduler, API `/jobs` | 009 |
| Accrual cuti | Accrual harian, top-up ulang tahun kerja, carry-over, hangus | 010 |
| AI BYOK | Tanpa paket; API key LLM per tenant (terenkripsi), toggle fitur, limit token | 011 |

## Sedang dikerjakan: report builder manual (ADR 012)

Sudah ada di commit terakhir, belum ada test khusus:

- Migrasi `report_template` + semantic views `v_employee`, `v_org_unit`, `v_leave_balance`,
  `v_leave_request` (`security_invoker = true`, wajib supaya RLS tetap berlaku).
- `app/reports/catalog.py`: dataset, kolom, label, tipe, deskripsi bisnis.
- `app/reports/query.py`: compiler spesifikasi terstruktur (kolom, filter, agregat, sort,
  limit) ke SQLAlchemy Core. Tidak ada SQL dari client.
- `app/reports/export.py`: CSV (BOM UTF-8) dan XLSX, anti formula injection.
- `app/services/reports.py` + `app/api/reports.py`: `/reports/datasets`, `/reports/query`,
  `/reports/export`, CRUD `/reports/templates` (+ run, export, Idempotency-Key).
- Smoke test manual di data perf (7.000 karyawan) sudah jalan: semua kolom katalog ada di view.

Langkah berikutnya, berurutan (ikuti skill `/new-module` langkah 5–7):

1. **Test `backend/tests/test_reports.py`:**
   - daftar dataset; setiap kolom katalog ada di view (query semua kolom);
   - query kolom + filter + sort; agregat (headcount per unit, total hari cuti per tipe);
   - periode default `leave_requests` (tanpa filter tanggal = tahun berjalan);
   - validasi 404/422: dataset/kolom tidak dikenal, operator salah tipe, nilai enum/tanggal
     salah, `between` butuh 2 nilai, sort bukan kolom terpilih, agregat salah tipe, kolom
     dobel, limit > 500;
   - upaya injection di nama kolom dan nilai filter; `contains` dengan `%` dan `_` literal;
   - `truncated` saat baris melebihi limit;
   - export CSV/XLSX: header, label enum, nama `=HYPERLINK(...)` tidak jadi formula, audit
     `report.export`; tolak > 10.000 baris (`report_too_large`, monkeypatch `EXPORT_LIMIT`);
   - template: create 201, nama dobel 409, spesifikasi invalid 422, pagination, update, run,
     export, delete, Idempotency-Key;
   - otorisasi: karyawan dan atasan 403 di semua endpoint;
   - isolasi tenant: HR tenant lain melihat hasil kosong dan template 404;
   - RLS di view: session app dengan tenant context A hanya melihat baris A, tanpa context 0.
2. **Performa:** di smoke test, agregasi `leave_requests` sekitar 0,9 detik dan select semua
   kolom `employees` sekitar 1,1 detik (cache dingin). Cek `EXPLAIN (ANALYZE, BUFFERS)` di data
   perf. Kemungkinan penyebabnya LATERAL jabatan per karyawan di `v_employee` yang ikut
   dihitung walau kolomnya tidak dipilih. Opsi: view ringan tanpa LATERAL untuk kolom
   pengajuan, atau summary table jabatan berlaku (SPEC: summary table). Perubahan view =
   migrasi baru, jangan edit migrasi yang sudah ter-commit.
3. **Dokumen:**
   - `mcp-server/README.md`: tambah `run_report` (`POST /reports/query`, HR, tanpa
     konfirmasi) dan `save_report_template` (`POST /reports/templates` + Idempotency-Key, HR,
     wajib konfirmasi);
   - `CLAUDE.md`: baris status (report builder selesai, berikutnya frontend/AI Assistant);
   - README: contoh curl laporan.
4. `make lint`, `make test`, `make openapi`, commit, push, pastikan CI hijau.

**Review manual** (menyentuh RLS dan otorisasi): view `security_invoker`, GRANT + RLS
`report_template`, dan role HR-only di `/reports`.

## Setelah report builder

Mengikuti fase di SPEC ("Fase development dengan Claude Code"):

- **Frontend:** form cuti, halaman approval, kalender tim, dashboard saldo, setting AI, report
  builder. UI lengkap tanpa AI; elemen AI mengikuti `ai_features` dari `GET /me`.
- **AI Assistant:**
  - `mcp-server/` dan `orchestrator/` (masih kosong);
  - endpoint chat AI di Core API yang meneruskan ke ai-gateway dengan key tenant, mencatat
    `ai_usage`, dan dijaga `require_feature` (ADR 011 poin 6);
  - tabel `ai_tool_call`, masking nama.
- **Reporting Agent:** LLM menghasilkan spesifikasi laporan yang sama (ADR 012 poin 7).

## Catatan teknis penting

- RLS: operator range `&&` tidak leakproof, jadi di bawah RLS tidak bisa jadi index condition.
  Query irisan tanggal memakai perbandingan tanggal biasa (ADR 008).
- Koneksi aplikasi lewat PgBouncer transaction mode: hanya `SET LOCAL` dan
  `pg_advisory_xact_lock`, jangan session-level.
- Koneksi owner/CLI melewati RLS: selalu filter `tenant_id` eksplisit.
- Data uji performa: `make seed-perf` (7.000 karyawan, ±120 ribu pengajuan cuti, 3 tahun).
- AI: `AI_ENABLED=false` di test. Untuk mencoba AI lokal isi `AI_SECRET_KEY` di `.env`
  (cara membuat ada di `.env.example`).
