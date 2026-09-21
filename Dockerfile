FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY tradingbot ./tradingbot
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

# non-root user
RUN useradd --create-home appuser
USER appuser

EXPOSE 8000

# Migrate, then serve (API + trading engine in one process, see docs).
ENTRYPOINT ["sh", "-c", "alembic upgrade head && uvicorn tradingbot.api.app:create_app --factory --host 0.0.0.0 --port ${API_PORT:-8000}"]
