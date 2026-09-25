# ADR 003: Celery untuk batch dan background job, bukan ARQ

- Status: diterima
- Tanggal: 2026-09-25

## Konteks

Sistem butuh job background: accrual dan reset saldo cuti, import massal, laporan besar,
notifikasi, refresh summary table, dan archiving. Job harus bisa dipecah per chunk dan dijalankan
paralel, di-retry, diprioritaskan per antrian, dan adil antar tenant (SPEC, "Batch dan background
process").

Draft awal SPEC memakai ARQ, tapi ARQ sekarang berstatus maintenance-only.

## Keputusan

Memakai **Celery 5 dengan Redis sebagai broker**:

- Tiga antrian: `high` (notifikasi), `default` (permintaan user), `low` (maintenance).
- Status job disimpan di tabel `job_run` PostgreSQL, bukan result backend Celery. Broker hanya
  jalur antrian, jadi Redis restart tidak menghilangkan jejak job.
- `acks_late` + `prefetch=1`, sehingga setiap job wajib idempotent dan bisa restart per chunk.
- Task Celery hanya pembungkus tipis. Logic job ada di `backend/app/jobs`.
- Scheduler kustom membaca `job_schedule` (bukan celery beat statis), supaya jadwal per tenant
  bisa diubah HR tanpa deploy.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Celery | Matang, chain/group untuk fan-out chunk, retry, routing antrian, banyak dipakai | Konfigurasi banyak, API sync (task async perlu jembatan) |
| ARQ | Ringan, native asyncio | Maintenance-only, fitur workflow terbatas |
| Dramatiq | API bersih, reliable | Ekosistem lebih kecil, fan-out/group kurang lengkap |
| Temporal | Workflow tahan crash, cocok proses panjang | Infra berat untuk MVP |

## Konsekuensi

- Worker memakai kode backend (path dependency), jadi logic tidak diduplikasi.
- Advisory lock di job harus `pg_advisory_xact_lock` selama koneksi lewat PgBouncer transaction mode.
- Autoscaling worker nanti memakai KEDA berdasarkan panjang antrian (tahap Kubernetes).
- RabbitMQ sebagai broker dievaluasi di tahap 3. Temporal dievaluasi untuk payroll (Fase 3).
