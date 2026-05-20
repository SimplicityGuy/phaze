FROM python:3.14-slim AS base

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./

# Install project
RUN uv sync --frozen --no-dev

# Prevent uv run from re-syncing at runtime
ENV UV_NO_SYNC=1

# Non-root user
RUN useradd -m -r phaze
USER phaze

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "phaze.main:app", "--host", "0.0.0.0", "--port", "8000"]
