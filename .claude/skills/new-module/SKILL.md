---
name: new-module
description: Urutan wajib membuat modul bisnis baru di backend (model → schema → service → router → test → MCP tool) dengan tenant_id + RLS, keyset pagination, require_feature untuk fitur AI, test isolasi tenant, dan cek EXPLAIN ANALYZE. Pakai setiap kali membuat modul/endpoint CRUD baru, misal "bikin modul leave_request" atau "tambah API employee".
argument-hint: <nama_modul>
---

# Membuat modul baru: $ARGUMENTS

Baca dulu bagian modul ini di `docs/SPEC.md` (data model, endpoint, MCP tool) dan cek `docs/decisions/`.
Kerjakan langkah di bawah **berurutan**. Jangan lompat ke langkah berikutnya sebelum checklist
langkah sekarang terpenuhi.

Helper yang sudah ada, pakai ini dan jangan bikin versi baru:

| Kebutuhan | Lokasi |
| --- | --- |
| Base model + mixin | `app.models.base`: `Base`, `UUIDPrimaryKeyMixin`, `TenantMixin`, `TimestampMixin` |
| User login | `app.api.deps.CurrentUserDep` (`user_id`, `tenant_id`, `roles`, `employee_id`) |
| Cek role | `app.api.deps.require_roles(Role.HR_ADMIN, ...)`, `app.models.Role` |
| Session DB + RLS | `app.api.deps.TenantSessionDep` (satu transaksi, tenant dari token) |
| Keyset pagination | `app.core.pagination.paginate`, `app.api.pagination.PageParamsDep` |
| Response list | `app.schemas.common.Page[T]` |
| Audit | `app.services.audit.record_audit` |
| Helper migrasi | `app.core.tenant`: `rls_statements`, `grant_statement` |
| Cek paket/fitur | `app.entitlement.deps.require_feature`, `app.entitlement.features.Feature` |

Contoh modul yang sudah mengikuti pola ini: `app/api/me.py`, `app/services/auth.py`,
`tests/test_rls.py` (isolasi tenant), `tests/test_auth.py`.

## 1. Model (`backend/app/models/<modul>.py`)

- [ ] Pakai `TenantMixin` (kolom `tenant_id` UUID `NOT NULL` + FK ke `tenant`) di setiap tabel.
- [ ] Composite index diawali `tenant_id`, sesuai pola query (misal `(tenant_id, employee_id, start_date)`).
- [ ] Data effective-dated (seperti `employee_job`) memakai `effdt` + `effseq`, tidak menimpa riwayat.
- [ ] Import model di `app/models/__init__.py` supaya terbaca Alembic.
- [ ] Migrasi baru: `cd backend && uv run alembic revision --autogenerate -m "<pesan>"`, lalu cek hasilnya.
- [ ] Tambahkan GRANT dan RLS di migrasi yang sama. Tanpa GRANT, role aplikasi tidak bisa
      mengakses tabel. Beri hak seminimal mungkin (misal tanpa DELETE kalau data cukup dinonaktifkan):

  ```python
  from app.core.tenant import grant_statement, rls_statements

  op.execute(grant_statement("<tabel>", "SELECT, INSERT, UPDATE"))
  for statement in rls_statements("<tabel>"):
      op.execute(statement)
  ```

- [ ] Jangan pernah mengedit migrasi yang sudah ter-commit. Hook akan menolak, buat migrasi baru.

## 2. Schema (`backend/app/schemas/<modul>.py`)

- [ ] Pydantic v2, terpisah dari model ORM: `<Nama>Create`, `<Nama>Update`, `<Nama>Read`.
- [ ] `tenant_id` tidak pernah diterima dari body request, selalu dari user login.
- [ ] Response list memakai `Page[<Nama>Read]`.
- [ ] Field sensitif (NIK KTP, gaji, rekening) tidak ada di schema MVP.

## 3. Service (`backend/app/services/<modul>.py`)

- [ ] Semua logic bisnis di sini, tidak bergantung pada FastAPI.
- [ ] Terima session dari `TenantSessionDep` (tenant context sudah di-set), **dan** tetap filter
      `tenant_id` di query. RLS adalah lapis kedua, dan koneksi admin/CLI melewati RLS.
- [ ] List memakai `paginate()`. Kolom sort NOT NULL, diakhiri primary key. Dilarang `OFFSET`.
- [ ] List transaksi default tahun berjalan, histori lama hanya lewat filter eksplisit.
- [ ] Pilih kolom yang dibutuhkan (tanpa `SELECT *`), pakai `selectinload`/`joinedload` (tanpa N+1).
- [ ] Hard validation lewat rules engine (`app/rules/`). Angka bisnis (saldo, jumlah hari)
      dihitung di sini, tidak pernah oleh LLM.
- [ ] Saldo disimpan dan di-update di transaksi yang sama, bukan dihitung ulang dari histori.
- [ ] Proses berat (import massal, hitung ulang massal) jadi job Celery, bukan di request.
- [ ] Perubahan data dicatat dengan `record_audit()` di transaksi yang sama (tanpa secret).

## 4. Router (`backend/app/api/<modul>.py`)

- [ ] Tipis: validasi input → panggil service → kembalikan schema. Tidak ada SQL di router.
- [ ] Endpoint plural kebab-case sesuai SPEC (misal `/leave/requests`).
- [ ] Pakai `CurrentUserDep` + `TenantSessionDep`, dan `require_roles(...)` sesuai tabel role di SPEC.
- [ ] List memakai `PageParamsDep` dan return `Page[...]`.
- [ ] Fitur AI (soft validation, ringkasan, reporting agent) wajib
      `dependencies=[Depends(require_feature(Feature.<FITUR_AI>))]`.
- [ ] Endpoint yang jadi aksi tulis via AI menerima header `Idempotency-Key`.
- [ ] Daftarkan router di `app/main.py`.

## 5. Test (`backend/tests/test_<modul>.py`)

Setiap endpoint wajib punya test untuk:

- [ ] Happy path.
- [ ] Validasi input (422) dan hard rules (ditolak rules engine).
- [ ] Otorisasi per role (karyawan, atasan, HR).
- [ ] **Isolasi tenant:** data tenant A tidak muncul untuk user tenant B (list kosong,
      get by id → 404, update/delete ditolak). Pakai fixture `make_tenant` dan `make_user`.
- [ ] Pagination: semua baris terambil tepat sekali lewat `next_cursor`.
- [ ] Fitur AI: 403 `feature_not_enabled` saat `AI_ENABLED=false`, fitur ERP tetap jalan.

Jalankan `make test` dan `make lint` sampai hijau.

## 6. Cek performa query

- [ ] Ambil SQL dari query baru:
      `str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))`.
- [ ] Jalankan `EXPLAIN (ANALYZE, BUFFERS) <sql>` di data seed 3 tahun
      (`make seed-perf`, lalu `docker compose exec postgres psql -U hrus -d hrus`).
- [ ] Pastikan memakai index (Index Scan / Index Only Scan), bukan Seq Scan di tabel besar.
- [ ] Selama `make seed-perf` belum diimplementasi: isi data dummy secukupnya, dan tulis di
      ringkasan ke user bahwa cek di data 3 tahun masih tertunda.

## 7. MCP tool

- [ ] Satu tool = pembungkus tipis satu endpoint, nama `kata_kerja_objek` sesuai tabel SPEC.
- [ ] Tool difilter per role, memakai token JWT user, aksi tulis wajib konfirmasi user.
- [ ] Selama `mcp-server/` belum di-scaffold: catat spec tool (nama, endpoint, role, butuh
      konfirmasi atau tidak) di bagian "Daftar tool" di `mcp-server/README.md`.

## Selesai

- [ ] `make lint` dan `make test` hijau.
- [ ] Kalau API berubah: `make openapi` supaya tipe frontend ikut ter-update.
- [ ] Ringkas ke user: file yang dibuat, hasil EXPLAIN ANALYZE, dan hal yang masih tertunda.
- [ ] Tandai jelas di ringkasan kalau diff menyentuh otorisasi, RLS, atau SQL Guard.
