# AI-Native HRIS

HRIS API-first dengan AI assistant opsional. Spec lengkap di [`docs/SPEC.md`](docs/SPEC.md),
aturan kerja di [`CLAUDE.md`](CLAUDE.md), keputusan arsitektur di [`docs/decisions/`](docs/decisions/).

## Prasyarat

- Docker + Docker Compose v2
- [uv](https://docs.astral.sh/uv/) (Python 3.12 di-install otomatis oleh uv)
- Node.js 22

## Mulai

```bash
make up        # build dan jalankan semua service, .env dibuat dari .env.example
               # buka http://localhost:8080
make migrate   # migrasi Alembic + user DB aplikasi (non-superuser, tunduk RLS)
make seed-dev  # tenant "demo": hr@demo.test, atasan@demo.test, karyawan@demo.test
               # password semua: demo-password
make test      # pytest backend, worker, ai-gateway (pakai postgres + redis dari compose)
make lint      # Ruff + ESLint + typecheck
make down      # hentikan semua service
```

Untuk dev lokal di luar Docker dan hook pre-commit: `make install`. Daftar lengkap: `make help`.

## Service

| Service | Port | Keterangan |
| --- | --- | --- |
| nginx | 8080 | `/api/*` ke backend (2 replika, `least_conn`), `/` ke frontend |
| backend-1, backend-2 | internal | Core API FastAPI, `/health` dan `/ready` |
| worker | - | Celery, antrian `high`, `default`, `low` |
| ai-gateway | internal | `POST /v1/chat`, aktif hanya jika `AI_ENABLED=true` |
| frontend | internal | Vite dev server |
| postgres | 5432 | PostgreSQL 16 + pgvector, database `hrus` dan `hrus_test` |
| pgbouncer | internal | Transaction pooling untuk aplikasi |
| redis | 6379 | Cache dan broker Celery |

Coba login lewat API:

```bash
curl -X POST http://localhost:8080/api/auth/login -H "Content-Type: application/json" \
  -d '{"tenant_slug":"demo","email":"hr@demo.test","password":"demo-password"}'
curl http://localhost:8080/api/me -H "Authorization: Bearer <access_token>"
```

Tenant baru untuk klien: `make create-tenant SLUG=acme NAME="PT Acme" EMAIL=hr@acme.co.id`.

AI default mati (`AI_ENABLED=false`). Untuk mencoba tanpa API key, set `AI_ENABLED=true` dan
`LLM_PROVIDER=mock` di `.env`.
