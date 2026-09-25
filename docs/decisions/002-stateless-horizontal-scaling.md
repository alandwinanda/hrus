# ADR 002: Semua service stateless untuk horizontal scaling

- Status: diterima
- Tanggal: 2026-09-25

## Konteks

Target satu tenant sampai 7.000+ karyawan, dengan puncak sekitar 700 user bersamaan. Beban
terberat adalah panggilan LLM (2–10 detik per respons) dan laporan besar, bukan CRUD. Deploy
awal satu VPS, lalu naik ke k3s dan multi-node (SPEC, "Skalabilitas dan load balancing").

Supaya bisa naik tahap tanpa rewrite, kapasitas harus bisa ditambah cukup dengan menambah replika.

## Keputusan

Semua service (Core API, AI Orchestrator, AI Gateway, worker) dibuat **stateless** sejak hari pertama:

- Tidak ada session, file, atau cache di memori atau disk lokal service. State ada di
  PostgreSQL, Redis, atau object storage (MinIO/S3).
- Semua konfigurasi lewat environment variable.
- Load balancer (nginx `least_conn`) tanpa sticky session. State percakapan AI disimpan di
  Redis/PostgreSQL.
- Health check `/health` (proses hidup) dan `/ready` (dependency terhubung), plus graceful shutdown.
- Job berat selalu lewat worker dan idempotent.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| Stateless + state eksternal | Tambah replika kapan saja, deploy tanpa memutus user | Butuh Redis/object storage sejak awal |
| Sticky session + state di memori | Sederhana di awal | Replika mati = sesi hilang, scaling tidak merata, rewrite saat pindah ke k3s |
| Scale vertikal saja | Tanpa perubahan arsitektur | Batas atas jelas, single point of failure |

## Konsekuensi

- Compose dev sudah menjalankan 2 replika backend di belakang nginx, jadi masalah state lokal
  ketahuan sejak awal.
- Setiap fitur baru tidak boleh menyimpan apa pun di memori proses antar request (termasuk
  cache in-process tanpa invalidasi lintas replika).
- Upload dan hasil laporan wajib ke object storage, bukan disk container.
- Redis jadi dependency wajib, perlu Sentinel di tahap high availability.
