# ── CSS build stage ──────────────────────────────────────────────────────────
# Compiles assets/src/app.css → src/phaze/static/css/app.css with the pinned
# standalone Tailwind v4 binary (NO Node). Replaces the former in-browser
# compiler (@tailwindcss/browser). Keep TAILWIND_VERSION in sync with the
# justfile `tailwind` recipe. The final image copies only the generated CSS.
FROM python:3.14-slim AS css-builder

ARG TAILWIND_VERSION=v4.3.2
ARG TARGETARCH

WORKDIR /build

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# TARGETARCH is buildx's amd64/arm64; map to Tailwind's x64/arm64 asset names.
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      "amd64") TW_ARCH="x64" ;; \
      "arm64") TW_ARCH="arm64" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 --retry-delay 5 \
        -o /usr/local/bin/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${TW_ARCH}"; \
    chmod +x /usr/local/bin/tailwindcss; \
    /usr/local/bin/tailwindcss --help >/dev/null

# app.css's @source scans ../../src/phaze/templates relative to the input file,
# so the templates must sit at that same path inside the stage.
COPY assets/ assets/
COPY src/phaze/templates/ src/phaze/templates/
RUN /usr/local/bin/tailwindcss \
        -i assets/src/app.css \
        -o src/phaze/static/css/app.css \
        --minify

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
COPY --from=ghcr.io/astral-sh/uv:0.11.24 /uv /uvx /bin/

# Install dependencies first (cache layer)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Copy source
COPY src/ src/
COPY alembic/ alembic/
COPY alembic.ini ./

# Build-time Tailwind CSS (replaces the in-browser compiler). Generated, not in
# the repo, so it is copied from the css-builder stage rather than the context.
COPY --from=css-builder /build/src/phaze/static/css/app.css src/phaze/static/css/app.css

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
