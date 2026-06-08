FROM python:3.14-slim AS base

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /bin/

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

# Non-root user pinned to uid/gid 1000 so the container can read media owned by
# uid 1000 (mode 700/770). The previous `-r` system account auto-assigned uid 999,
# which could not read uid-1000-owned files and silently produced 0-file scans.
RUN groupadd -g 1000 phaze && useradd -m -u 1000 -g 1000 phaze
USER phaze

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "phaze.main:app", "--host", "0.0.0.0", "--port", "8000"]
