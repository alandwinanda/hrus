.DEFAULT_GOAL := help
.PHONY: help env install up down logs ps test test-backend test-worker test-ai-gateway lint format migrate seed-perf openapi

COMPOSE := docker compose
PY_SERVICES := backend worker ai-gateway

help: ## Tampilkan daftar perintah
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-16s %s\n", $$1, $$2}'

env: ## Buat .env dari .env.example kalau belum ada
	@test -f .env || (cp .env.example .env && echo ".env dibuat dari .env.example")

install: ## Install dependency lokal (uv + npm) dan hook pre-commit
	@for s in $(PY_SERVICES); do (cd $$s && uv sync) || exit 1; done
	cd frontend && npm ci
	uvx pre-commit install

up: env ## Jalankan semua service (build ulang kalau ada perubahan)
	$(COMPOSE) up -d --build --wait
	@echo "Buka http://localhost:8080 (atau port NGINX_PORT di .env)"

down: ## Hentikan semua service (data postgres tetap ada)
	$(COMPOSE) down

logs: ## Ikuti log semua service
	$(COMPOSE) logs -f --tail=100

ps: ## Status service
	$(COMPOSE) ps

test: env ## Jalankan semua test (butuh postgres + redis dari compose)
	$(COMPOSE) up -d --wait postgres redis
	$(MAKE) test-backend test-worker test-ai-gateway

test-backend:
	cd backend && uv run pytest

test-worker:
	cd worker && uv run pytest

test-ai-gateway:
	cd ai-gateway && uv run pytest

lint: ## Ruff (Python) + ESLint, Prettier, dan typecheck (frontend)
	@for s in $(PY_SERVICES); do (cd $$s && uv run ruff check . && uv run ruff format --check .) || exit 1; done
	cd frontend && npm run lint && npm run format:check && npm run typecheck

format: ## Rapikan kode dengan Ruff (Python) dan Prettier (frontend)
	@for s in $(PY_SERVICES); do (cd $$s && uv run ruff check --fix . && uv run ruff format .) || exit 1; done
	cd frontend && npm run format

migrate: ## Jalankan migrasi Alembic (langsung ke postgres, tidak lewat PgBouncer)
	$(COMPOSE) exec backend-1 alembic upgrade head

seed-perf: ## Seed data uji performa 7.000 karyawan x 3 tahun (belum diimplementasi)
	@echo "seed-perf belum diimplementasi. Lihat docs/SPEC.md bagian Performa query jangka panjang."

openapi: ## Generate tipe TypeScript frontend dari OpenAPI backend
	cd backend && uv run python -c "import json; from app.main import app; print(json.dumps(app.openapi(), indent=2))" > ../frontend/openapi.json
	cd frontend && npm run gen:api
