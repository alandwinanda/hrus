# Build context: root repo, karena worker memakai kode backend/.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

COPY backend/pyproject.toml backend/uv.lock /srv/backend/
COPY worker/pyproject.toml worker/uv.lock /srv/worker/
WORKDIR /srv/worker
RUN uv sync --frozen --no-dev --no-install-project --no-install-package hrus-backend

COPY backend /srv/backend
COPY worker /srv/worker
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 app
USER app

CMD ["celery", "-A", "worker.celery_app", "worker", "--queues=high,default,low", "--concurrency=2", "--loglevel=INFO"]
