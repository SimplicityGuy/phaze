FROM python:3.13-slim AS base

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Install system dependency: curl for model downloads
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Download models (cache layer -- changes rarely)
COPY scripts/download_models.sh scripts/
RUN bash scripts/download_models.sh /models

# Copy source
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./

# Install project
RUN uv sync --frozen --no-dev

# Non-root user
RUN useradd -m -r phaze
USER phaze

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "phaze.main:app", "--host", "0.0.0.0", "--port", "8000"]
