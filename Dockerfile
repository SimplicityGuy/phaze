FROM python:3.14-slim AS base

WORKDIR /app

# Audio pipeline native system deps. Must run as root, so it stays before
# `USER phaze` below. essentia-tensorflow's native `_essentia` extension links
# libatomic.so.1 (libatomic1); the decode/fingerprint toolchain needs ffmpeg +
# ffprobe (ffmpeg), libsndfile.so.1 (libsndfile1), and fpcalc + libchromaprint.so.1
# (libchromaprint-tools). Without these, `import essentia` fails at runtime and
# every analysis job dead-letters.
# libpq5 (v4.1.1): provides libpq.so.5 for psycopg's SAQ PostgresQueue broker (Phase 36).
# psycopg[binary] bundles its own libpq, but libpq5 is a belt-and-suspenders fallback for
# the pure-Python psycopg path — without a libpq backend, `import phaze.main` crash-loops
# with `ImportError: no pq wrapper available` (the v4.1.0 regression).
# DL3008: versions are intentionally unpinned — Debian-slim apt package versions
# shift on every base-image refresh and pinning them would break builds on each
# security update. The base image tag controls the package snapshot instead.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Install uv
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /uvx /bin/

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
