-- Hanya jalan sekali saat volume postgres masih kosong (dev lokal).
-- Di production, extension dibuat lewat migrasi Alembic.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- Database terpisah untuk pytest.
CREATE DATABASE hrus_test;
\connect hrus_test
CREATE EXTENSION IF NOT EXISTS vector;
