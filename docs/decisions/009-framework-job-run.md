# ADR 009: Framework batch job (job_run, chunk, scheduler)

- Status: diterima
- Tanggal: 2026-09-28

## Konteks

SPEC ("Batch dan background process") mewajibkan proses berat lewat job, bukan di request API:

- `job_run` di PostgreSQL jadi sumber kebenaran status, bukan broker.
- Job dipecah per chunk (misal 500 karyawan), commit per chunk, idempotent, dan bisa restart dari
  chunk yang gagal.
- Hanya satu run per tenant dan jenis job, ada dry-run, dan jadwal per tenant bisa diubah HR.

Batasan dari keputusan sebelumnya:

- Koneksi aplikasi lewat PgBouncer transaction mode, jadi tidak boleh ada session advisory lock
  (ADR 003).
- Role aplikasi tunduk RLS, jadi tidak bisa melihat data tenant lain (ADR 006).

## Keputusan

1. **Tabel `job_schedule`, `job_run`, `job_run_chunk`, `job_run_log`** memakai `tenant_id`, RLS,
   dan composite FK. `job_run_log` append-only.
2. **Definisi job ada di kode** (`app/jobs/definitions.py`), bukan tabel `job_definition`.
   Definisi berisi kode, antrian, parameter (Pydantic), `prepare`, `plan`, `run_chunk`, batas
   percobaan, dan cron bawaan. Kode dan definisi tidak mungkin beda versi.
   `GET /jobs/definitions` menampilkan daftarnya.
3. **Alur run:**
   - API atau scheduler membuat run `queued`. Parameter disimpan setelah `prepare` (misal `as_of`
     default hari ini), supaya retry memakai nilai yang sama.
   - Worker `start_run` menjalankan `plan` dan menyimpan chunk. Status run jadi `running`.
   - Tiap chunk dikerjakan task terpisah.
   - Run selesai sebagai `success`, `partial` (sebagian chunk gagal), atau `failed`.
4. **Satu chunk = satu transaksi** berisi perubahan data bisnis dan status chunk. Baris chunk
   dikunci `FOR UPDATE SKIP LOCKED`, jadi task ganda (redelivery Celery, kiriman ulang scheduler)
   tidak pernah mengulang perubahan.
   - Chunk gagal dicoba lagi dengan jeda 10, 20, 40 detik sampai `max_attempts`, lalu berstatus
     `failed`.
   - `POST /jobs/runs/{id}/retry` hanya mengulang chunk yang gagal.
5. **Satu run aktif per tenant + jenis job** dijaga unique index parsial
   `(tenant_id, job_code) WHERE status IN ('queued', 'running')`. Ini pengganti advisory lock:
   tetap berlaku lintas worker dan restart, dan aman di PgBouncer.
6. **Dry-run menjalankan logic yang sama di savepoint lalu di-rollback.** Hasilnya berupa jumlah
   per jenis perubahan di `job_run.output`, dan detail per chunk (maksimal 1.000 per chunk) di
   `GET /jobs/runs/{id}/chunks`.
7. **Task dikirim setelah commit** (BackgroundTasks di API, setelah transaksi di scheduler/worker).
   Kalau broker mati, run tetap tercatat dan scheduler mengirim ulang.
8. **Scheduler kustom** (`python -m app.jobs.scheduler`, service `scheduler`):
   - Tiap 30 detik mengambil `pg_try_advisory_xact_lock`, jadi aman dijalankan 2 replika.
   - Pekerjaan lintas tenant ditemukan lewat fungsi `SECURITY DEFINER job_scheduler_work()`, yang
     hanya mengembalikan `kind`, `tenant_id`, dan `id`. Pekerjaan selanjutnya dilakukan per tenant
     di bawah RLS.
   - Menangani tiga kasus: jadwal jatuh tempo, run `queued` > 5 menit (task hilang), dan run
     `running` tanpa kemajuan > 30 menit (chunk pending dikirim ulang).
   - Jadwal yang terlewat tidak dikejar satu per satu. Jadwal saat run sebelumnya masih jalan
     dilewati.
9. **Task Celery tipis** (`worker/worker/tasks.py`) dan memakai engine tanpa pool per task, karena
   `asyncio.run` membuat event loop baru. Pooling tetap di PgBouncer.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Status di tabel + chunk transaksional (dipilih) | Tahan restart Redis/worker, restart per chunk, bisa diaudit | Lebih banyak kode dibanding Celery murni |
| Result backend Celery + chord | Sedikit kode | Status hilang kalau Redis restart, chord rapuh |
| Advisory lock per job | Sesuai teks SPEC | Session lock tidak aman di PgBouncer, xact lock tidak bisa menjangkau banyak chunk |
| Celery beat | Bawaan Celery | Jadwal statis, satu instance, tidak per tenant |
| Tabel `job_definition` | Bisa dilihat dari SQL | Bisa beda versi dengan kode, perlu disinkronkan |
| Scheduler pakai koneksi owner | Sederhana | Kredensial BYPASSRLS ada di service yang selalu hidup |

## Konsekuensi

- Job baru cukup menambah `JobDefinition` di `app/jobs/definitions.py`. Logic bisnisnya ditulis
  di service, dan `run_chunk` wajib idempotent.
- Role owner migrasi wajib punya BYPASSRLS (sama seperti CLI admin), supaya fungsi scheduler
  melihat semua tenant.
- Belum ada batas run bersamaan per tenant lintas jenis job (SPEC: fair antar tenant), antrian
  per pool terpisah, read replica untuk job berat, dan alert dead-letter. Chunk `failed` sudah
  tersimpan dan bisa jadi dasar alert. Ditambah saat jumlah job bertambah.
- Process Monitor (UI) dan MCP tool `run_job` / `get_job_status` memakai endpoint `/jobs`.
