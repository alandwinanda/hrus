# ADR 005: Pilihan teknis scaffold monorepo

- Status: diterima
- Tanggal: 2026-09-25

## Konteks

Scaffold awal mengikuti `docs/SPEC.md` dan `CLAUDE.md`. Beberapa detail teknis tidak diatur di SPEC,
jadi dicatat di sini supaya tidak berubah diam-diam.

## Keputusan

1. **uv + `pyproject.toml` per service.** `worker/` memakai `backend/` sebagai path dependency
   (editable), jadi model, service, dan logic job tidak diduplikasi. Build Docker worker memakai
   root repo sebagai context.
2. **psycopg 3 sebagai driver PostgreSQL.** Async di backend, sync di Alembic dan nanti di job worker.
   Prepared statement dimatikan (`prepare_threshold=None`) supaya aman di PgBouncer transaction mode.
3. **Migrasi langsung ke PostgreSQL** (`MIGRATION_DATABASE_URL`), tidak lewat PgBouncer.
4. **Tenant context per transaksi** dengan `set_config('app.tenant_id', ..., true)`, setara `SET LOCAL`.
5. **Dua replika backend ditulis eksplisit** (`backend-1`, `backend-2`), bukan `deploy.replicas`,
   supaya daftar upstream nginx (`least_conn`) stabil.
6. **Logging JSON dengan structlog.** Kode logging dan middleware request_id disalin di
   `ai-gateway/` karena belum ada folder shared library. Kalau salinan bertambah, buat ADR baru
   untuk shared package.
7. **Status job di tabel `job_run`**, bukan result backend Celery (`task_ignore_result=True`).
   `acks_late` + `prefetch=1`, jadi setiap job wajib idempotent.
8. **ai-gateway memanggil provider lewat httpx** (format OpenAI `/chat/completions`), tanpa SDK
   provider. Model dipilih lewat env, bukan oleh client.
9. **Tipe TypeScript di-generate dari OpenAPI** (`make openapi`). `openapi-typescript` dijalankan
   lewat `npx` karena peer dependency-nya belum mendukung TypeScript 6.

## Konsekuensi dan pekerjaan lanjutan

- Aplikasi harus konek dengan role database **non-superuser** saat tabel dan RLS pertama dibuat,
  karena superuser selalu melewati RLS. Di dev sekarang masih memakai `POSTGRES_USER` (superuser).
- Advisory lock di job harus memakai varian transaksi (`pg_advisory_xact_lock`) selama koneksi
  lewat PgBouncer transaction mode.
- Extension `vector` dan `pg_stat_statements` dibuat oleh `infra/postgres/init.sql` untuk dev.
  Untuk production, pindahkan ke migrasi Alembic.
