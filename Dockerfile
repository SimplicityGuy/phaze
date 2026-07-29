# ── CSS build stage ──────────────────────────────────────────────────────────
# Compiles assets/src/app.css → src/phaze/static/css/app.css with the pinned
# standalone Tailwind v4 binary (NO Node). Replaces the former in-browser
# compiler (@tailwindcss/browser). Keep TAILWIND_VERSION *and* the two
# TAILWIND_SHA256_LINUX_* digests below in sync with the justfile `tailwind`
# recipe -- the version tag alone is not an integrity guarantee (phaze-hvzd):
# a git tag can be moved and a release asset can be replaced without the
# version string changing, so the digest is the thing that actually pins the
# binary that runs as root during `docker build`. Digests are the
# `tailwindcss-linux-{x64,arm64}` entries from the upstream release's
# `sha256sums.txt` for TAILWIND_VERSION. The final image copies only the
# generated CSS.
FROM python:3.14-slim AS css-builder

ARG TAILWIND_VERSION=v4.3.2
ARG TAILWIND_SHA256_LINUX_X64=5036c4fb4328e0bcdbb6065c70d8ac9452e0d4c947113a788a8f94fd390425c1
ARG TAILWIND_SHA256_LINUX_ARM64=394ddccc2402cfa3abd97dfba56f3587781a3d6e6ce66e65ceada14beb7664b8
ARG TARGETARCH

WORKDIR /build

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# TARGETARCH is buildx's amd64/arm64; map to Tailwind's x64/arm64 asset names.
# Download to a temp path and verify its digest with sha256sum -c BEFORE
# chmod +x or promoting it to the path we execute -- a mismatch never yields
# an executable file (phaze-hvzd: no checksum meant a compromised release
# asset would run as root inside the build with only a `--help` liveness
# check, which does not fail on a malicious replacement).
# hadolint ignore=DL4006
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
      "amd64") TW_ARCH="x64"; TW_SHA256="${TAILWIND_SHA256_LINUX_X64}" ;; \
      "arm64") TW_ARCH="arm64"; TW_SHA256="${TAILWIND_SHA256_LINUX_ARM64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 5 \
        -o /tmp/tailwindcss \
        "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-${TW_ARCH}"; \
    echo "${TW_SHA256}  /tmp/tailwindcss" | sha256sum -c - \
        || { echo "❌ tailwindcss-linux-${TW_ARCH} failed checksum verification" >&2; rm -f /tmp/tailwindcss; exit 1; }; \
    mv /tmp/tailwindcss /usr/local/bin/tailwindcss; \
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
# rsync + openssh-client (phaze-w64q): the `io` lane's `push_file` task
# (src/phaze/tasks/push.py) shells out to `rsync -e "ssh -i …"` for the
# cloud-burst push pipeline (docker-compose.agent.yml worker-io). Phase 50/51
# assumed these binaries would be provisioned on the sending image but never
# added them — every push terminalized with FileNotFoundError. The `command -v`
# assertion below turns a future slimming regression into a build failure
# instead of a silent per-file runtime failure.
# DL3008: versions are intentionally unpinned — Debian-slim apt package versions
# shift on every base-image refresh and pinning them would break builds on each
# security update. The base image tag controls the package snapshot instead.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 ffmpeg libsndfile1 libchromaprint-tools libpq5 rsync openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && command -v rsync ssh

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
