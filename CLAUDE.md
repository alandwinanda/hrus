# CLAUDE.md

## Project

AI-Native HRIS: HRIS SaaS multi-tenant untuk perusahaan di Indonesia (50–7.000+ karyawan per tenant).
Semua fitur dibangun API-first, dan UI maupun AI assistant memakai API yang sama. AI opsional dan
bukan sumber kebenaran: tanpa paket, tenant memakai API key LLM sendiri (BYOK, ADR 011).

Scope MVP: Core HR, Leave Management, report builder manual, setting AI (BYOK), AI Assistant,
AI form validation, Reporting Agent. Payroll, attendance, dan fitur lain di luar MVP (lihat SPEC).

**Acuan utama: `docs/SPEC.md`.** Kalau ada yang tidak jelas atau bertentangan dengan file ini,
ikuti SPEC dan tanyakan dulu sebelum mengubah desain.

Status: fondasi Core API (auth JWT, role, tenant, RLS, audit_log), Core HR, Leave (saldo, pengajuan,
approval, kalender, accrual, carry-over), framework job, dan setting AI BYOK sudah ada.
Berikutnya: report builder manual. Keputusan teknis: ADR 005–011. Job baru: ADR 009.

## Perintah

```
make up / down     jalankan / hentikan semua service (http://localhost:8080)
make test          pytest backend, worker, ai-gateway (postgres + redis dari compose)
make lint          Ruff + ESLint + Prettier + tsc   make format   rapikan kode (Ruff + Prettier)
make migrate       Alembic + user DB aplikasi     make seed-dev  tenant demo (hr@demo.test)
make openapi       generate tipe TS frontend dari OpenAPI backend
```

Test satu service: `cd backend && uv run pytest tests/test_health.py`.

## Struktur monorepo

```
backend/       Core API (FastAPI): Core HR, Leave, rules engine, entitlement, endpoint laporan
worker/        Celery worker + scheduler: accrual, import, laporan besar, notifikasi
ai-gateway/    Satu-satunya pintu ke LLM provider: adapter, allowlist URL, masking (stateless)
orchestrator/  AI agent + tool calling, chat streaming (SSE), Reporting Agent
mcp-server/    MCP tools, masing-masing pembungkus tipis satu endpoint Core API
frontend/      Vite + React + TypeScript + Tailwind (form, chat, dashboard)
infra/         Docker Compose, Nginx, script deploy dan backup (k3s nanti)
docs/          SPEC.md dan decisions/ (ADR)
```

- Setiap service punya Dockerfile, dependency (uv, `pyproject.toml`), dan test sendiri.
- `worker/` memakai model dan service dari `backend/`. Logic bisnis tidak boleh diduplikasi.
- Backend: `app/{api,core,models,schemas,services,rules,jobs,entitlement}`. Pakai helper yang ada:
  `api/deps.py` (`CurrentUserDep`, `TenantSessionDep`, `require_roles`), `core/pagination.py`,
  `core/tenant.py` (RLS + helper migrasi), `core/errors.py`, `services/audit.py`, `entitlement/`.

## Konvensi Python

- Python 3.12, type hints wajib di semua fungsi public.
- Ruff untuk lint dan format (`ruff check`, `ruff format`), wajib bersih sebelum commit.
- FastAPI: router tipis → service (logic bisnis) → query/repository. Router tidak berisi SQL.
- Pydantic v2: `ConfigDict`, `model_validate`, `model_dump`. Jangan pakai API v1
  (`orm_mode`, `.dict()`, `parse_obj`).
- Skema request/response terpisah dari model ORM, misal `LeaveRequestCreate`, `LeaveRequestRead`.
- SQLAlchemy 2 style: `Mapped[...]`, `mapped_column`, `select()`. Jangan pakai `session.query()`.
- Semua perubahan skema lewat migrasi Alembic. Tidak ada `create_all` di luar test.
- Celery: task tipis, logic ada di modul job/service. Parameter task berupa ID dan nilai JSON,
  bukan object ORM.
- pytest: test di `tests/` tiap service. Test DB pakai PostgreSQL asli (bukan SQLite) supaya RLS
  ikut teruji.

Penamaan:

- Tabel: snake_case singular (`leave_request`, `employee_job`), sesuai SPEC.
- Endpoint: plural, kebab-case (`/leave/requests`, `/leave/team-calendar`).
- MCP tool: snake_case `kata_kerja_objek` (`get_leave_balance`, `submit_leave_request`).

## Konvensi TypeScript

- TypeScript `strict`. Dilarang `any`, pakai `unknown` + narrowing.
- React function component + hooks, satu komponen per file, nama file komponen PascalCase.
- Styling hanya Tailwind. Chart pakai Recharts. Chat streaming via SSE.
- ESLint wajib bersih sebelum commit.
- Tipe request/response diambil dari OpenAPI Core API, jangan ditulis ulang manual.
- UI tetap lengkap tanpa AI. Elemen AI tampil hanya kalau ada di `ai_features` dari `GET /me`.

## Aturan arsitektur

1. **Multi-tenant.** Semua tabel bisnis punya `tenant_id NOT NULL` + GRANT + RLS + composite FK
   `(tenant_id, x_id)`. Aplikasi konek sebagai user non-superuser, tenant context dari token JWT.
   Setiap fitur baru wajib punya test kebocoran antar tenant.
2. **Satu codebase.** SaaS dan dedicated memakai kode yang sama, bedanya hanya
   `DEPLOYMENT_MODE=saas|dedicated` dan konfigurasi deploy. Dilarang cabang kode per klien.
3. **Stateless.** Tidak ada session, file, atau cache di memori atau disk lokal service.
   State ada di PostgreSQL, Redis, atau object storage. Semua config lewat env var.
4. **LLM tidak boleh menghitung angka bisnis** (saldo, jumlah hari, total). Semua angka berasal
   dari hasil tool, yaitu Core API.
5. **Orchestrator tidak boleh akses database langsung.** Semua data lewat MCP tools → Core API.
   Pengecualian dari SPEC: Reporting Agent boleh read-only ke read replica, hanya lewat semantic
   views + SQL Guard.
6. **Token user, bukan service account.** Orchestrator dan MCP meneruskan JWT user (on-behalf-of).
   Otorisasi selalu diputuskan Core API.
7. **Aksi tulis via AI wajib konfirmasi user** (submit, cancel, approve, simpan template,
   jalankan job), plus idempotency key supaya tidak double submit.
8. **Validasi dua lapis.** Hard rules di rules engine backend (blokir). AI hanya soft validation
   (warning dan saran). Form dan chat memakai endpoint `validate` yang sama.
9. **Audit.** Setiap tool call AI dicatat di `ai_tool_call`, setiap perubahan data di `audit_log`.

## Aturan AI opsional

- Semua fitur ERP wajib jalan penuh dengan `AI_ENABLED=false`. Test suite backend wajib lulus
  di mode ini.
- Kalau fitur AI mati, limit token habis, atau provider gagal, aplikasi kembali ke mode ERP.
  Form tetap bisa submit dengan hard validation.
- Semua panggilan LLM wajib lewat `ai-gateway/`. Service lain dilarang import SDK provider atau
  memanggil API LLM langsung.
- Provider, model, dan API key per tenant (`tenant_ai_setting`, diatur HR, default `deepseek-flash`).
  Key terenkripsi (`AI_SECRET_KEY`), tidak pernah dikembalikan, di-log, atau diaudit.
- Fitur AI dicek dengan dependency `require_feature()` di backend, lalu juga di daftar MCP tools
  dan UI. Menyembunyikan tombol di UI saja tidak cukup.
- NIK, gaji, dan nomor rekening tidak pernah dikirim ke LLM. Nama karyawan di-mask sebelum
  dikirim dan dikembalikan setelah respons.
- Setiap panggilan LLM dicatat lewat `services/ai_usage.record_usage` (limit token bulanan).
- System prompt dan definisi tool selalu di awal dan tidak berubah per request (cache-friendly).

## Aturan performa

- Composite index selalu diawali `tenant_id`.
- Keyset pagination. Dilarang `OFFSET` untuk list besar. Semua endpoint list wajib pagination.
- Dilarang `SELECT *` dan N+1. Pilih kolom yang dibutuhkan, pakai `selectinload`/`joinedload`.
- List transaksi default tahun berjalan. Histori lama hanya lewat filter eksplisit.
- Saldo disimpan, bukan dihitung ulang: `leave_balance` di-update di transaksi yang sama.
- Query baru wajib dicek `EXPLAIN ANALYZE` di seed 3 tahun (7.000 karyawan).
- Aturan lain (summary table, cache, statement timeout, partisi): lihat SPEC bagian
  "Performa query jangka panjang".

## Aturan batch

- Proses berat (accrual, import, laporan besar, notifikasi massal, refresh summary) wajib lewat
  job Celery, tidak boleh jalan di dalam request API.
- API hanya membuat record `job_run` lalu enqueue. `job_run` di PostgreSQL adalah sumber
  kebenaran status job, bukan broker.
- Job wajib chunked (misal 500 karyawan per chunk), idempotent, commit per chunk, dan bisa
  restart dari chunk yang gagal.
- Satu run aktif per tenant + jenis job (unique index parsial `job_run`, ADR 009). Accrual dan
  import wajib mendukung dry-run. Job baru = `JobDefinition` di `app/jobs/definitions.py`.

## Umum

- Setiap endpoint wajib ada test: happy path, validasi, otorisasi per role, isolasi tenant.
- Jangan commit secret. Pakai `.env` (di-gitignore) dan `.env.example` sebagai template.
- Cek `docs/decisions/` sebelum mengubah arsitektur. Perubahan arsitektur ditulis sebagai ADR baru
  di sana dulu (salin `000-template.md`).
- Modul baru wajib lewat skill `/new-module`: model → schema → service → router → test → MCP tool.
- Hook di `.claude/` otomatis memformat file (Ruff, Prettier) dan menolak edit `.env` serta migrasi
  Alembic yang sudah ter-commit. Jangan diakali, buat migrasi baru.
- Diff yang menyentuh otorisasi, RLS, atau SQL Guard wajib ditandai jelas untuk review manual.
