# One image for api, worker and scheduler. Build context is the repo root:
#   docker build -f api/Dockerfile --target runtime --build-arg GIT_SHA=$(git rev-parse --short HEAD) .

FROM python:3.12-slim-bookworm AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv
# pg_dump / pg_restore 16 for the nightly backup (Debian bookworm ships 15, which cannot dump 16)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-16 \
    && apt-get purge -y curl gnupg && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
RUN useradd --system --uid 10001 --no-create-home app

# Bake the embedding model into the image: no download at first use, no Hugging Face dependency
# at runtime. Change EMBEDDING_MODEL here and in .env together.
ARG EMBEDDING_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_MODEL=$EMBEDDING_MODEL \
    EMBEDDING_CACHE_DIR=/opt/models
RUN python -c "import os; from fastembed import TextEmbedding; TextEmbedding(os.environ['EMBEDDING_MODEL'], cache_dir='/opt/models')" \
    && chmod -R a+rX /opt/models

# ---- dev: includes test/lint tooling; source is bind-mounted by docker-compose.override.yml
FROM base AS dev
COPY alembic.ini ./
COPY api ./api
RUN uv sync --frozen
USER app
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- runtime: production image
FROM base AS runtime
COPY alembic.ini ./
COPY api ./api
RUN uv sync --frozen --no-dev
ARG GIT_SHA=unknown
ENV GIT_SHA=$GIT_SHA
USER app
EXPOSE 8000
# Only Caddy can reach this port (no published ports), so trusting forwarded headers is safe.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]
