# ADR 001: pgvector sebagai vector store

- Status: diterima
- Tanggal: 2026-09-25

## Konteks

Fitur "tanya aturan perusahaan" (RAG) butuh embedding dokumen policy per tenant dan pencarian
kemiripan. Volume dokumen per tenant kecil (puluhan sampai ratusan dokumen peraturan), sedangkan
tim kecil dan deploy awal hanya satu VPS dengan Docker Compose (lihat SPEC, tahap scaling 1).

Data policy juga harus terisolasi per tenant, sama seperti data HR lainnya.

## Keputusan

Embedding disimpan di PostgreSQL dengan extension **pgvector**, di database yang sama dengan
data utama. Tabel `policy_document` (dan tabel chunk-nya nanti) punya `tenant_id` + RLS seperti
tabel lain.

## Alternatif yang dipertimbangkan

| Opsi | Kelebihan | Kekurangan |
| --- | --- | --- |
| pgvector | Satu database, backup dan RLS ikut sistem yang sama, join langsung dengan data HR | Performa di jutaan vektor di bawah vector DB khusus |
| Qdrant / Weaviate | Cepat di skala besar, fitur filter vektor lengkap | Service tambahan, backup dan isolasi tenant harus diurus terpisah |
| Pinecone (managed) | Tanpa operasional | Data keluar ke pihak ketiga, biaya per tenant, tidak bisa untuk deployment dedicated |

## Konsekuensi

- Infra lebih ringkas: tidak ada service baru, backup pgBackRest juga mencakup embedding.
- Isolasi tenant memakai RLS yang sama, jadi tidak ada jalur bocor data lewat vector store.
- Deployment dedicated tidak butuh komponen tambahan.
- Index vektor (HNSW) menambah beban tulis dan memori PostgreSQL. Embedding ulang dijalankan
  sebagai job Celery di luar jam peak.
- Ditinjau ulang kalau total vektor melewati beberapa juta atau latency pencarian p95 > 200 ms.
