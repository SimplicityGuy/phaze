# Phaze - Music alignment tool
# Run `just` to see all available commands

# phaze-tcqq: single source of truth for the pinned Postgres image used by every
# justfile-launched Postgres container (test-db, integration-test, perf-db-up). Before
# this variable existed the tag was hardcoded at 8 separate justfile sites (4 real
# `docker run` arguments + 4 echo strings that only CLAIM the version), so a partial
# bump could print the old tag while running the new one -- a divergence that survives
# casual review because the log output looks right. docker-compose.yml and
# .github/workflows/tests.yml cannot read a justfile variable, so they keep their own
# literal pins; tests/agents/deployment/test_postgres_image_pin.py is the mechanical guard that
# keeps all three in step -- bump this value AND the other two pins together, or the
# guard test fails and names the mismatch.
postgres_image := "postgres:18-alpine"
# phaze-knwk: match production's docker-compose.yml `shm_size: "256m"` on every
# justfile-launched Postgres container (test-db, integration-test, perf-db-up), so a
# harness run cannot pass on a Postgres feature (a bigger parallel index build, a
# manually-raised work_mem/maintenance_work_mem) that the 64 MB Docker default would
# reject in production, or vice versa. See docker-compose.yml's `postgres.shm_size`
# comment for the investigation and derivation.
postgres_shm_size := "256m"
# Host bind IP for every ephemeral/test-harness Postgres + Redis container this justfile
# publishes (test-db, integration-test's pinned-port branches, perf-db-up). Defaults to
# loopback-only (phaze-v7ki): without a bind IP, `docker run -p PORT:PORT` binds 0.0.0.0
# (dual-stack, so also `::`), publishing the shared test Postgres (superuser phaze/phaze)
# and a passwordless test Redis to every host on the LAN. Mirrors the loopback pattern the
# `integration-test` dynamic-port branches already use (`-p 127.0.0.1::5432`).
test_db_bind_ip := env_var_or_default("PHAZE_TEST_DB_BIND_IP", "127.0.0.1")
# Host port for the SHARED test-harness Postgres (5433 avoids the dev DB on 5432). This
# container is deliberately reused across every concurrent worktree/session (via `test-db`,
# `test-db-for`, and `check`) -- see phaze-20vd/phaze-pik6 for the concurrency invariants that
# protect it. `integration-test` does NOT use this container; it has its own dedicated pair below.
test_db_port := env_var_or_default("PHAZE_TEST_DB_PORT", "5433")
# Fixed container name for the SHARED test-harness Postgres
test_db_container := "phaze-test-db"
# Host port for the SHARED test-harness Redis (6380 avoids a dev Redis on 6379)
test_redis_port := env_var_or_default("PHAZE_TEST_REDIS_PORT", "6380")
# Fixed container name for the SHARED test-harness Redis
test_redis_container := "phaze-test-redis"
# Logical database count on the test Redis. Redis defaults to 16; we raise it so the per-worktree
# index space (DB 0 is the allocation registry, seats get 1..N-1) comfortably exceeds any realistic
# concurrent-seat count. `just test-db-for <name>` allocates out of this space.
test_redis_databases := env_var_or_default("PHAZE_TEST_REDIS_DATABASES", "64")
# Dedicated, disposable Postgres + Redis for `just integration-test` ONLY (phaze-pik6). A SEPARATE
# container pair (own names + ports) so integration-test's auto-teardown EXIT trap can never
# `docker rm -f` the SHARED phaze-test-db/phaze-test-redis harness other concurrent worktrees rely
# on -- the same isolation principle as perf_db_container below, applied to the one-shot test path.
# phaze-987z: these are NAME PREFIXES, not fixed names -- the recipe below appends a
# per-invocation unique token (shell PID + $RANDOM) so two concurrent `integration-test` runs
# never share a container name and can never `docker rm -f` each other's containers. Host ports
# default to a dynamically-assigned free port (read back via `docker port`); set
# PHAZE_INTEGRATION_TEST_DB_PORT/_REDIS_PORT to pin a fixed port instead (e.g. for a debugger
# that needs a stable address -- note a pinned port reintroduces the two-concurrent-runs race,
# so only pin it for a single deliberate invocation).
integration_db_container_prefix := "phaze-integration-test-db"
integration_db_port := env_var_or_default("PHAZE_INTEGRATION_TEST_DB_PORT", "0")
integration_redis_container_prefix := "phaze-integration-test-redis"
integration_redis_port := env_var_or_default("PHAZE_INTEGRATION_TEST_REDIS_PORT", "0")
# Dedicated ephemeral Postgres for the Phase-82 PERF-02 /pipeline/stats bench. A SEPARATE container
# (own port 5545) so an explicit `just test-db-down`/`test-db` recreate on the shared phaze-test-db
# (e.g. from a sibling session) can never wipe the ~200K seeded perf corpus mid-measurement.
perf_db_container := "phaze-perf-db"
perf_db_port := env_var_or_default("PHAZE_PERF_DB_PORT", "5545")
perf_db_name := "phaze_perf82"
perf_db_dsn := "postgresql://phaze:phaze@localhost:" + perf_db_port + "/" + perf_db_name
perf_db_sa_dsn := "postgresql+asyncpg://phaze:phaze@localhost:" + perf_db_port + "/" + perf_db_name
# Standalone Tailwind CSS binary version. Keep in sync with the Dockerfile
# css-build stage. NO Node — the standalone binary compiles assets/src/app.css.
tailwind_version := "v4.3.2"
# Per-platform sha256 digests for the {{ tailwind_version }} standalone binary, taken from
# upstream's own `sha256sums.txt` release asset. The `tailwind` recipe verifies the download
# against these BEFORE chmod +x/promoting it (phaze-hvzd) -- without this, a compromised
# release asset would be downloaded once, cached at ./bin/tailwindcss, and reused indefinitely
# on an operator machine with no re-check. Keep in sync with the Dockerfile css-builder ARGs.
tailwind_sha256_linux_x64 := "5036c4fb4328e0bcdbb6065c70d8ac9452e0d4c947113a788a8f94fd390425c1"
tailwind_sha256_linux_arm64 := "394ddccc2402cfa3abd97dfba56f3587781a3d6e6ce66e65ceada14beb7664b8"
tailwind_sha256_macos_x64 := "cef8f110471e889c3c4409055cf8aff33076f58a081867b0dfc6534b290bfbb0"
tailwind_sha256_macos_arm64 := "b800b0659dc64b9f03ede5660244d9415d777d5739ae2889280877ca37be742a"
# Port the api service is published on (docker-compose.yml: "${API_PORT:-8000}:8000"). Keep
# this in sync with any API_PORT override passed to `just up`/`up-dev`.
api_port := env_var_or_default("API_PORT", "8000")
# The production entrypoint (src/phaze/entrypoint.py) unconditionally serves HTTPS with a
# self-signed internal CA (phaze-a9rr) -- there is no plain-HTTP branch, so operator recipes
# that curl the API must use https:// and trust this CA, same as docs/quick-start.md and
# README.md already do.
api_ca_cert := env_var_or_default("PHAZE_API_CA_CERT", "certs/phaze-ca.crt")
api_base := "https://localhost:" + api_port

[doc('Install all dependencies')]
[group('dev')]
install: tailwind
    uv sync

# phaze-gfdx: this is the bh worktree-provisioning entry point (global beadhive config's
# `worktree.init` runs `just setup` whenever a justfile is present), so it MUST be cheap and
# MUST NOT fail when GitHub is unreachable. Deliberately does NOT depend on `tailwind` (that
# recipe curls the standalone Tailwind binary from GitHub releases, chmods/verifies it, then
# compiles app.css -- slow, and a real provisioning failure if GitHub is down) and deliberately
# does NOT alias `install`. `setup` is the automation path; `install` remains the full human
# local-dev bootstrap (tailwind + uv sync) and is left unchanged.
#
# Does NOT run `pre-commit install`: bh worktrees share the main clone's .git/hooks (no
# per-worktree core.hooksPath), so installing from inside an ephemeral worktree overwrites the
# MAIN clone's shared hook and hardcodes THIS worktree's own .venv path into it as
# INSTALL_PYTHON. Once this worktree is torn down (which bh does routinely after submit) that
# path is dangling, breaking the hook for the main clone and every other worktree sharing it --
# and two worktrees provisioning concurrently would race to stomp each other's hook. Verified
# live in this worktree (phaze-gfdx): `uv run pre-commit install` reported success and wrote
# .git/hooks/pre-commit in the MAIN clone with INSTALL_PYTHON pointing at this worktree's
# ephemeral .venv. A `setup` that silently wires a hook this fragile is worse than one that
# only syncs deps, so it's left out; `just pre-commit` (run-all-files, no install) and manual
# `uv run pre-commit install` remain available for whoever wants hooks in a durable checkout.
[doc('Prepare a working copy for bh worktree provisioning: sync deps only (cheap, no network beyond PyPI, safe to re-run)')]
[group('dev')]
setup:
    uv sync

[doc('Start all services in Docker (production topology: base compose only)')]
[group('dev')]
up: tailwind
    # phaze-he8m: pre-create the ./certs bind-mount source owned by the invoking
    # operator (uid 1000). Without it, rootful dockerd auto-creates the missing
    # source dir as root:root and the uid-1000 cert bootstrap dies with
    # PermissionError writing /certs/phaze-ca.crt before uvicorn ever binds.
    mkdir -p certs
    # phaze-476w: pass -f docker-compose.yml EXPLICITLY so the dev overlay is
    # NEVER auto-merged. A bare `docker compose up` auto-merges the old
    # docker-compose.override.yml, which replaced the api command with plain-HTTP
    # `uvicorn --reload` and skipped the cert-bootstrap entrypoint. Use `just
    # up-dev` for the live-reload dev overlay.
    docker compose -f docker-compose.yml up -d

[doc('Start all services with the live-reload DEV overlay (docker-compose.dev.yml)')]
[group('dev')]
up-dev: tailwind
    # phaze-476w: the dev overlay (plain-HTTP uvicorn --reload, ./src bind mount,
    # PHAZE_DEBUG=true) is now opt-in and included EXPLICITLY here — it is no
    # longer auto-merged into `just up`. It deliberately skips the cert bootstrap.
    mkdir -p certs
    docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

[doc('Start file-server agent stack (standalone docker-compose.agent.yml)')]
[group('dev')]
up-agent:
    # phaze-he8m: pre-create ./models and ./certs so the uid-1000 worker can
    # auto-download models and read the CA (avoids a root-owned daemon-created dir).
    mkdir -p models certs
    docker compose -f docker-compose.agent.yml up -d

[doc('Start the OCI A1 cloud compute-agent stack (standalone docker-compose.cloud-agent.yml)')]
[group('dev')]
cloud-agent-up:
    # phaze-he8m: pre-create ./models and ./certs owned by the operator (uid 1000).
    mkdir -p models certs
    docker compose -f docker-compose.cloud-agent.yml up -d

[doc('Stop the OCI A1 cloud compute-agent stack')]
[group('dev')]
cloud-agent-down:
    docker compose -f docker-compose.cloud-agent.yml down

[doc('Start both stacks on one host (developer convenience)')]
[group('dev')]
up-all:
    # phaze-he8m: pre-create ./certs (api cert bootstrap) and ./models (agent
    # model auto-download) owned by the operator (uid 1000) before the daemon
    # auto-creates them root:root.
    mkdir -p certs models
    docker compose -f docker-compose.yml -f docker-compose.agent.yml up -d

[doc('Stop all services')]
[group('dev')]
down:
    docker compose down

[doc('View logs for all services (follow mode)')]
[group('dev')]
logs:
    docker compose logs -f

[doc('Rebuild and restart services')]
[group('dev')]
rebuild: tailwind
    # phaze-ckui (phaze-476w class): pass -f docker-compose.yml EXPLICITLY, same as
    # `up`/`up-dev`/`up-all` -- a bare `docker compose up` auto-merges a stray
    # docker-compose.override.yml if one exists in the repo root.
    docker compose -f docker-compose.yml up -d --build

[doc('Download the standalone Tailwind binary (NO Node) and rebuild app.css')]
[group('build')]
tailwind:
    @mkdir -p src/phaze/static/css bin
    # phaze-y3iyt: the download used to be gated on EXISTENCE only (`[ ! -x ./bin/tailwindcss ]`),
    # so bumping tailwind_version (+ the sha256 pins) had no effect on any machine already holding
    # a cached binary -- every `just tailwind`/`up`/`up-dev`/`rebuild` kept compiling app.css with
    # the stale version forever, silently diverging from the Dockerfile css-builder stage (whose
    # cache key is the Docker layer, busted automatically by its ARG). Stamp the version that was
    # actually verified and installed next to the binary, and re-download whenever it doesn't
    # match the currently configured pin -- this also closes the residual phaze-hvzd hole where a
    # binary cached before the sha256 pins existed was never re-checked against any digest.
    @if [ ! -x ./bin/tailwindcss ] || [ "$(cat ./bin/tailwindcss.version 2>/dev/null || true)" != "{{ tailwind_version }}" ]; then \
        echo "⬇️  Downloading standalone Tailwind binary ({{ tailwind_version }})..."; \
        OS=$(uname -s | tr '[:upper:]' '[:lower:]' | sed 's/darwin/macos/'); \
        ARCH=$(uname -m | sed 's/x86_64/x64/;s/aarch64/arm64/'); \
        case "${OS}-${ARCH}" in \
            "linux-x64") TW_SHA256="{{ tailwind_sha256_linux_x64 }}" ;; \
            "linux-arm64") TW_SHA256="{{ tailwind_sha256_linux_arm64 }}" ;; \
            "macos-x64") TW_SHA256="{{ tailwind_sha256_macos_x64 }}" ;; \
            "macos-arm64") TW_SHA256="{{ tailwind_sha256_macos_arm64 }}" ;; \
            *) echo "❌ no pinned sha256 for ${OS}-${ARCH}; refusing to download unverified" >&2; exit 1 ;; \
        esac; \
        rm -f ./bin/tailwindcss.tmp ./bin/tailwindcss.version; \
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --retry-delay 5 -o ./bin/tailwindcss.tmp \
            "https://github.com/tailwindlabs/tailwindcss/releases/download/{{ tailwind_version }}/tailwindcss-${OS}-${ARCH}" \
        && { \
            if command -v sha256sum >/dev/null 2>&1; then \
                echo "${TW_SHA256}  ./bin/tailwindcss.tmp" | sha256sum -c -; \
            elif command -v shasum >/dev/null 2>&1; then \
                echo "${TW_SHA256}  ./bin/tailwindcss.tmp" | shasum -a 256 -c -; \
            else \
                echo "❌ neither sha256sum nor shasum available to verify download" >&2; exit 1; \
            fi; \
        } \
        && chmod +x ./bin/tailwindcss.tmp \
        && ./bin/tailwindcss.tmp --help >/dev/null \
        && mv ./bin/tailwindcss.tmp ./bin/tailwindcss \
        && echo "{{ tailwind_version }}" > ./bin/tailwindcss.version \
        || { echo "❌ Tailwind download or verification failed; removing partial binary" >&2; rm -f ./bin/tailwindcss.tmp ./bin/tailwindcss.version; exit 1; }; \
    fi
    ./bin/tailwindcss -i assets/src/app.css -o src/phaze/static/css/app.css --minify

[doc('Run all tests')]
[group('test')]
test:
    uv run pytest tests/ -x -q

[doc('Run tests with coverage report')]
[group('test')]
test-cov:
    uv run pytest --cov=phaze --cov-report=term-missing

[doc('Run tests with coverage XML output (for CI)')]
[group('test')]
test-ci:
    uv run pytest --cov=phaze --cov-report=xml --cov-report=term-missing

[doc('Run a specific test file')]
[group('test')]
test-file FILE:
    uv run pytest {{FILE}} -x -v

# phaze-tzy6s.14: the real-browser suite. Boots the actual app (uvicorn + real lifespan + real
# Alembic migrations) against its OWN database, derived from this worktree's TEST_DATABASE_URL seat
# by appending `_browser`, and drives it with Playwright.
#
# Playwright is deliberately NOT a project dependency (only Patchright is — it is needed at RUNTIME
# by the 1001Tracklists render path; see the pyproject mypy-override comment). This suite therefore
# uses the ephemeral `uv run --with` idiom, the same one scripts/analyze_browser_soak.py uses. The
# browsers themselves are a one-off install:
#
#     just test-browser-install     # once per machine
#     just test-db                  # the shared Postgres/Redis harness must be up
#     just test-browser
#
# `-m browser` overrides the `addopts = "-m 'not browser'"` default that keeps this suite out of a
# bare `uv run pytest`.
[doc('Install the Chromium build the browser suite drives (run once per machine)')]
[group('test')]
test-browser-install:
    uv run --with playwright playwright install --with-deps chromium

# Depends on `tailwind` deliberately. Without the compiled src/phaze/static/css/app.css the app
# serves an UNSTYLED page: every asset request 404s and every layout assertion (drawer visibility,
# horizontal overflow, touch targets) becomes meaningless while still reporting green, because an
# unstyled document trivially satisfies "does not overflow". The first run of this suite passed the
# overflow test for exactly that reason. A browser suite with no CSS is worse than none.
[doc('Run the real-browser Playwright suite (needs `just test-db` up; excluded from the default run)')]
[group('test')]
test-browser: tailwind
    uv run --with playwright --with pytest-asyncio --with asyncpg pytest tests/browser -m browser -q

# Non-blocking dead-code sweep (CLEAN-02). NOT a CI/pre-commit gate — framework-invoked
# code produces false-positives that need per-candidate human reachability judgment. A
# nonzero exit merely lists remaining candidates to hand-verify. vulture_whitelist.py is a
# hand-audited suppression list for framework false-positives (FastAPI handlers, Pydantic
# validators, transient ORM attrs, SAQ hooks, CLI entry points).
[doc('Run the vulture dead-code sweep over src/phaze (non-blocking; lists candidates to hand-verify)')]
[group('test')]
vulture:
    uv run vulture src/phaze vulture_whitelist.py --min-confidence 80 --ignore-decorators "@router.*,@app.*,@field_validator,@model_validator,@validator,@pytest.fixture"

# --cov-fail-under=0 is REQUIRED: a single shard only exercises a fraction of
# phaze, so pytest-cov auto-enforcing pyproject's global fail_under gate against a
# shard's PARTIAL coverage would fail every leg (exit 1) before the shard is uploaded,
# and the combine job (needs: [test]) would never run. The global gate is enforced
# once, on the COMBINED number, by `coverage-combine`.
#
# PATHS is one or more space-separated `tests/...` paths (phaze-crq9k: the CI matrix is
# driven off tests/ci_shards.json, whose entries can split a single tests/<bucket>
# directory into several parallel shards, e.g. "tests/shared/core" and
# "tests/shared/routers tests/shared/services ..."). NAME is only the shard label used
# for the .coverage.<NAME> shard filename -- it no longer has to equal a directory name.
[doc('Run a single CI shard (one or more test paths), writing coverage data to .coverage.<name>. XDIST="" keeps DB shards serial; DB-free shards pass XDIST="-n auto".')]
[group('test')]
test-bucket NAME PATHS XDIST="":
    COVERAGE_FILE=.coverage.{{NAME}} uv run pytest {{PATHS}} {{XDIST}} --cov=phaze --cov-report= --cov-fail-under=0 -q

[doc('Combine per-bucket .coverage.* shards into coverage.xml and enforce the gate')]
[group('test')]
coverage-combine:
    uv run coverage combine
    uv run coverage xml
    uv run coverage json
    uv run coverage report --fail-under=95
    uv run python scripts/coverage_floor.py

# phaze-2rgq2: refreshing the coverage repowise folds into its health scores is a FIVE-command
# path, and two of those commands are invisible from the tools' own output. Encoding it here is
# the point of the recipe. Verifying it end to end turned up two more, so there are now THREE
# silent-partial shapes and one command whose green output means nothing. Read all of it before
# changing any of it -- every claim below was measured on this repo, not inferred.
#
# WHY BOTH INGESTS. `repowise coverage add .coverage` and
# `coverage xml && repowise coverage add coverage.xml` are NOT alternatives -- they populate two
# DIFFERENT tables, and doing only the first is a silent partial success. The .coverage report
# carries per-test CONTEXTS but no per-file line totals, so that ingest builds the test-to-code map
# (29317 test->file records when this was measured) and nothing else. It prints "Built the
# test-to-code map: N test->file record(s)" and `repowise coverage status` then renders a populated
# map, so every surface says success -- while `line_coverage_pct` stays NULL, `get_health` reports
# coverage {file_count: 0}, and the `untested_hotspot` biomarker keeps firing on files that are
# 96-100% covered. Only the Cobertura coverage.xml populates the per-file table (249 files, 98.4%).
# Conversely coverage.xml carries no contexts, so it can never build the map -- and an empty map
# makes `get_risk`'s tests_to_run and the impacted-tests skill return nothing, which reads as "no
# tests" when it means "unknown". Hence: both, always, and the run FAILS if the second one does
# not land. Exiting 0 with only the map is precisely the state that looks correct while leaving
# every health score wrong.
#
# WHY THE COMMIT PAIRING. Health findings carry line numbers. An index built at commit A paired
# with coverage measured at commit B maps coverage onto lines that have moved. The suite takes ~21
# minutes -- long enough that `main` advanced twice within an hour during the session that filed
# this bead -- so the script pins HEAD before the run, refuses on modified tracked files, warns on
# untracked ones, and fails if HEAD moved by the end.
#
# `ingested_commit_sha` is NOT the field that settles this, however much it looks like it.
# repowise stamps it from `repositories.head_commit`, which `repowise init` sets and
# `repowise update` never refreshes -- it advances `churn_anchor_sha` and state.json's
# `last_sync_commit` instead. Measured here: an update to 85111c59, with git HEAD and
# last_sync_commit BOTH at 85111c59, still stamped a days-old 1c85e2ec on a correct 249-of-249
# ingest. The first version of this gate failed on that and was wrong to -- it would red every
# incrementally-updated repo. The gate now pairs on evidence repowise does maintain
# (`last_sync_commit` == HEAD) plus INGEST FRESHNESS (both `ingested_at` newer than the run's
# start), and treats a stale sha as a note. A MISSING sha still fails: that shape is a broken
# repository registration, and it is how the first verification run got caught.
#
# TRAP 3, AND IT IS WORSE THAN TRAP 1. `repowise coverage add` maps the report's paths onto the
# files it has INDEXED, keeps whatever maps, prints "N report file(s) did not map to the repo tree"
# as a NOTE, and exits 0. Measured: a run that mapped 1 of 249 files and 26 of 29,334 test->file
# pairs reported success on every surface. Trap 1 leaves `line_coverage_pct` NULL, which reads as
# missing; trap 3 fills it in with a plausible number computed from almost nothing, which reads as
# FINE. A reader who only knows traps 1 and 2 will believe a wrong percentage. Hence the gate takes
# `--coverage-xml` and fails when fewer files mapped than the report contains. (Cause, if you hit
# it: repowise keys its index on the repo's ABSOLUTE path. A checkout at a new path gets a NEW,
# EMPTY repository row from `repowise update`, and coverage then maps against that.)
#
# `repowise status` IS NOT A VALIDITY CHECK, and this is the one to remember when wiring CI
# (phaze-6it9k). During the 1-of-249 run it reported `indexed: true`, the CORRECT
# `last_sync_commit`, and `file_count: 2251` -- every signal green while the ingest could map 9
# files. `repowise health --format json` is no better: it re-parses the working tree and reported
# `file_count: 2251` against a repository row holding nothing. `repowise context` on a known file
# also still succeeds. Nothing available fails early, which is why this recipe fails LATE, after
# the suite, by deliberate design rather than omission -- and why the gate reads the stored state
# back instead of trusting an exit code. `repowise coverage add` exits 0 even when it ingests
# NOTHING and prints "No indexed files found -- run `repowise init` first"; verified directly.
#
# Per-file health is `repowise health --file <path> --format json` (or the MCP `get_health`). The
# positional argument to `repowise health` is a REPO PATH, so `repowise health src/phaze/foo.py`
# fails with `Not a directory: .../foo.py/.repowise` and then prints a misleading "Healthy 10.0/10".
[doc('Refresh repowise: reindex, run the suite with per-test coverage contexts, ingest BOTH coverage artifacts, fold into health. ~21 min. Pass a seat name to auto-provision an isolated test DB.')]
[group('test')]
repowise-coverage seat="":
    @bash scripts/repowise-coverage.sh "{{seat}}"

[doc('Classify changed files (newline-delimited on stdin) as code-changed=true|false for the CI doc-only skip gate (CI-04)')]
[group('test')]
detect-code-changes:
    @bash scripts/classify-changed-files.sh

[doc('Start ephemeral Postgres + Redis for integration tests (ports PHAZE_TEST_DB_PORT/PHAZE_TEST_REDIS_PORT, defaults 5433/6380)')]
[group('test')]
test-db:
    #!/usr/bin/env bash
    set -euo pipefail
    container="{{test_db_container}}"
    port="{{test_db_port}}"
    redis_container="{{test_redis_container}}"
    redis_port="{{test_redis_port}}"
    # Race-safe bootstrap (phaze-20vd): this recipe is invoked concurrently from multiple
    # worktrees, so the create path must never `docker rm -f` a container we merely
    # observed as absent -- a sibling invocation may have created (or be about to create)
    # it in the window between our inspect and our own action, and `rm -f`ing it would wipe
    # that sibling's freshly-provisioned databases out from under it. Instead: try `docker
    # start` (a no-op success if a stopped container of this name already exists), then
    # fall back to `docker run`. If a racing sibling's `docker run` wins in that same
    # window, ours fails with docker's "name already in use" -- that is the expected LOSER
    # path here, not a fatal error: fall through (via run_or_yield below) and let the
    # readiness wait further down confirm the winner's container came up.
    run_or_yield() {
        local name="$1" verb="$2"
        shift 2
        local run_err
        run_err="$(mktemp)"
        if docker run -d --name "$name" "$@" >/dev/null 2>"$run_err"; then
            rm -f "$run_err"
            return 0
        fi
        if grep -q "is already in use" "$run_err"; then
            echo "🔁 ${name} was ${verb} by a concurrent invocation; continuing"
            rm -f "$run_err"
            return 0
        fi
        cat "$run_err" >&2
        rm -f "$run_err"
        return 1
    }
    # phaze-3yznp: `docker start` resurrects a container with whatever image/port it was
    # ORIGINALLY created with -- it carries none of the currently-configured knobs (unlike
    # `docker run`, which gets every one as an argument). A container left over from before a
    # postgres_image bump, or created under a different PHAZE_TEST_DB_PORT/
    # PHAZE_TEST_REDIS_PORT, is silently reused while the surrounding echo asserts the
    # CURRENTLY configured values -- and the in-container pg_isready probe below is blind to
    # the host-port mismatch. Verify a reused container's actual image/port before trusting it.
    verify_reused_container() {
        local name="$1" want_image="$2" want_hostport="$3" container_port="$4"
        local got_image got_hostport
        got_image="$(docker inspect -f '{{{{.Config.Image}}' "$name" 2>/dev/null || echo '')"
        got_hostport="$(docker port "$name" "$container_port" 2>/dev/null | head -n1 | sed -E 's/.*:([0-9]+)$/\1/')"
        if [ "$got_image" != "$want_image" ] || [ "$got_hostport" != "$want_hostport" ]; then
            echo "❌ Existing ${name} does not match the configured image/port." >&2
            echo "   configured: image=${want_image} host-port=${want_hostport}" >&2
            echo "   actual:     image=${got_image:-<unknown>} host-port=${got_hostport:-<unknown>}" >&2
            echo "   'docker start' reuses whatever image/port the container was created with;" >&2
            echo "   it does not pick up a postgres_image bump or a port override." >&2
            echo "   Run 'just test-db-down' (PHAZE_TEST_DB_FORCE_DOWN=1 if it reports busy), then retry." >&2
            exit 1
        fi
    }
    if [ "$(docker inspect -f '{{{{.State.Running}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
        verify_reused_container "$container" "{{postgres_image}}" "$port" "5432/tcp"
        echo "🐘 ${container} already running on port ${port}"
    else
        echo "🐘 Starting ${container} ({{postgres_image}}) on host port ${port}..."
        if docker start "$container" >/dev/null 2>&1; then
            verify_reused_container "$container" "{{postgres_image}}" "$port" "5432/tcp"
        else
            run_or_yield "$container" "created" \
                -e POSTGRES_USER=phaze \
                -e POSTGRES_PASSWORD=phaze \
                -e POSTGRES_DB=phaze_test \
                --shm-size {{postgres_shm_size}} \
                -p "{{test_db_bind_ip}}:${port}:5432" \
                {{postgres_image}}
        fi
    fi
    redis_databases="{{test_redis_databases}}"
    redis_running="$(docker inspect -f '{{{{.State.Running}}' "$redis_container" 2>/dev/null || echo false)"
    redis_reused=0
    if [ "$redis_running" = "true" ]; then
        redis_reused=1
    else
        echo "🟥 Starting ${redis_container} (redis:7-alpine, ${redis_databases} logical DBs) on host port ${redis_port}..."
        if docker start "$redis_container" >/dev/null 2>&1; then
            redis_reused=1
        fi
        redis_running="$(docker inspect -f '{{{{.State.Running}}' "$redis_container" 2>/dev/null || echo false)"
    fi
    if [ "$redis_running" = "true" ] && [ "$redis_reused" = "1" ]; then
        verify_reused_container "$redis_container" "redis:7-alpine" "$redis_port" "6379/tcp"
    fi
    if [ "$redis_running" = "true" ]; then
        # A container started before this setting existed (or with a smaller value) only has 16
        # logical databases. Recreate it rather than silently handing out indices it cannot address.
        # This check applies whether the container was already running or was just reused via
        # `docker start` above -- either way it now genuinely exists, so removing it here is a
        # deliberate resize, never a speculative rm racing a sibling's in-flight create.
        current_databases="$(docker exec "$redis_container" redis-cli CONFIG GET databases 2>/dev/null | tail -n1 || echo 0)"
        if [ "${current_databases:-0}" -ge "$redis_databases" ]; then
            echo "🟥 ${redis_container} running on port ${redis_port} (${current_databases} logical DBs)"
        else
            echo "♻️  ${redis_container} has only ${current_databases:-0} logical DBs (need ${redis_databases}); recreating."
            echo "    This CLEARS the test Redis, including per-worktree DB allocations. Re-run"
            echo "    'just test-db-for <name>' in each active worktree afterwards."
            # phaze-1t4gc: this resize is a deliberate rm (see the comment above), but unlike
            # test-db-down (phaze-ieqg) it had no live-seat guard at all -- so raising
            # PHAZE_TEST_REDIS_DATABASES (including via the exhaustion remedy test-db-for's own
            # error message suggests) would silently wipe every concurrent worktree's live Redis
            # keys and the DB-index registry mid-suite. Every live suite also holds a Postgres
            # advisory-lock backend (tests/db_guard.py), so the same pg_stat_activity probe
            # test-db-down uses is a valid liveness signal here too.
            if [ "${PHAZE_TEST_DB_FORCE_DOWN:-}" != "1" ] && \
               [ "$(docker inspect -f '{{{{.State.Running}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
                busy="$(docker exec "$container" psql -U phaze -d postgres -tAc \
                    "SELECT string_agg(DISTINCT datname || '  (backend pid ' || pid || ', ' || coalesce(nullif(application_name, ''), 'unnamed client') || ')', chr(10) || '     ')
                       FROM pg_stat_activity
                      WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND datname LIKE 'phaze%'" 2>/dev/null || true)"
                if [ -n "$(printf '%s' "$busy" | tr -d '[:space:]')" ]; then
                    echo "❌ Refusing to recreate ${redis_container} for the logical-DB resize: another seat is using the shared harness." >&2
                    echo "     ${busy}" >&2
                    echo "" >&2
                    echo "   Recreating now would wipe every concurrent worktree's live Redis keys and the" >&2
                    echo "   DB-index allocation registry out from under those runs -- the same false-red" >&2
                    echo "   signature test-db-down's guard exists to prevent (phaze-ieqg / phaze-1t4gc)." >&2
                    echo "" >&2
                    echo "   If you are here because allocation ran out of indices, you almost certainly do not" >&2
                    echo "   need this resize at all -- \`just test-db-reclaim\` frees the seats that are no longer" >&2
                    echo "   in use with no teardown, and \`just test-db-seats\` shows what it would free (phaze-68wky)." >&2
                    echo "" >&2
                    echo "   Otherwise wait for those runs to finish, or PHAZE_TEST_DB_FORCE_DOWN=1 just test-db-for ..." >&2
                    echo "   if you know the connections are stale." >&2
                    exit 1
                fi
            fi
            docker rm -f "$redis_container" >/dev/null 2>&1 || true
            run_or_yield "$redis_container" "recreated" \
                -p "{{test_db_bind_ip}}:${redis_port}:6379" \
                redis:7-alpine redis-server --databases "$redis_databases"
        fi
    else
        # Neither running nor startable (no container of this name existed) -- create fresh,
        # tolerating a racing sibling's concurrent create as described above.
        run_or_yield "$redis_container" "created" \
            -p "{{test_db_bind_ip}}:${redis_port}:6379" \
            redis:7-alpine redis-server --databases "$redis_databases"
    fi
    echo "⏳ Waiting for Postgres to accept connections..."
    for _ in $(seq 1 30); do
        # phaze-cbf1r: probe over TCP (`-h 127.0.0.1`), not the default unix socket. The
        # postgres entrypoint's first-boot sequence runs a TEMPORARY, socket-only server
        # (`listen_addresses=''`) to create phaze_test and run init scripts before starting
        # the real server -- a socket probe reports OK against that temp server too (PQping
        # only distinguishes "no server answering" from "a server answered"), so the
        # unqualified probe could break out of this loop before phaze_test genuinely exists,
        # intermittently failing the next step (scripts/ensure-pg-database.sh). The temp
        # server never listens on TCP, so `-h 127.0.0.1` is exactly the discriminator between
        # "a server is up" and "the final server, with phaze_test, is up".
        if docker exec "$container" pg_isready -h 127.0.0.1 -U phaze -d phaze_test >/dev/null 2>&1; then
            db_ready=1
            break
        fi
        sleep 1
    done
    if [ "${db_ready:-0}" != "1" ]; then
        echo "❌ ${container} did not become ready within 30s" >&2
        docker logs "$container" >&2 || true
        exit 1
    fi
    echo "⏳ Waiting for Redis to accept connections..."
    for _ in $(seq 1 30); do
        if docker exec "$redis_container" redis-cli ping >/dev/null 2>&1; then
            redis_ready=1
            break
        fi
        sleep 1
    done
    if [ "${redis_ready:-0}" != "1" ]; then
        echo "❌ ${redis_container} did not become ready within 30s" >&2
        docker logs "$redis_container" >&2 || true
        exit 1
    fi
    # phaze-hk8r: tolerate a lost create race against a concurrent `test-db`/`test-db-for`/
    # `check` invocation -- see scripts/ensure-pg-database.sh's header.
    bash scripts/ensure-pg-database.sh "$container" phaze_migrations_test
    echo "✅ ${container} ready on localhost:${port} (phaze_test + phaze_migrations_test)"
    echo "✅ ${redis_container} ready on localhost:${redis_port}"

[doc('Create a correctly-named isolated test DB pair for one worktree, e.g. `just test-db-for laqf`')]
[group('test')]
test-db-for name:
    #!/usr/bin/env bash
    set -euo pipefail
    # Exists so nobody hand-rolls an isolated database name again. The natural instinct is to
    # SUFFIX the standard name (`phaze_test_<name>`); that shape is accepted by the guard in
    # `tests/db_guard.py`, but this recipe emits the canonical `phaze_<name>_test` pair and,
    # more importantly, prints the exact exports to use. Requires `just test-db` first.
    just test-db
    container="{{test_db_container}}"
    port="{{test_db_port}}"
    raw_name="{{name}}"
    # phaze-fmfk: a raw Postgres unquoted identifier can't contain a hyphen, but this repo's own
    # worktree convention IS hyphenated (`wt/bead/issue/phaze-fq9h-1`), so naming a seat after the
    # worktree -- the obvious, natural thing to do -- used to blow up with a raw
    # `syntax error at or near "-"` and no hint at the cause. `derive-seat-name.sh` is the single
    # place that decides what a raw name normalizes to (validation rule + collision-safe hashing);
    # see its header for the full rationale. Shared with this script's own regression tests so the
    # justfile recipe and the tests can never drift apart.
    name="$(bash scripts/derive-seat-name.sh "$raw_name")"
    echo "Seat '${raw_name}' -> identifier '${name}' (stable across re-runs of the same seat name)."
    main_db="phaze_${name}_test"
    migrations_db="phaze_${name}_migrations_test"
    # phaze-hk8r: ensure-pg-database.sh tolerates a lost create race -- see its header for why
    # a plain SELECT-then-CREATE is a check-then-act TOCTOU that used to kill this recipe under
    # `set -e` when a concurrent invocation (e.g. a dispatcher and an agent both recovering
    # exports for one worktree at once) won the CREATE first.
    bash scripts/ensure-pg-database.sh "$container" "$main_db" "$migrations_db"
    # Redis isolation. Postgres-only isolation was the phaze-fwo7 defect: every worktree landed on
    # the same logical Redis DB 0, where fixtures run global `scan_iter`+`delete` sweeps over
    # `exec:*` / `tracklist_req:*` and assertions count the global keyspace. One seat's cleanup then
    # deletes another seat's live keys mid-test, producing failures indistinguishable from a real
    # regression. Allocation is an atomic registry in DB 0, NOT a hash of the name: hash % N collides
    # ~35% of the time across 8 seats, which would reintroduce the bug intermittently.
    redis_container="{{test_redis_container}}"
    redis_port="{{test_redis_port}}"
    redis_databases="{{test_redis_databases}}"
    # phaze-68wky: allocation lives in `scripts/redis-seat-registry.sh` (one atomic server-side
    # script; see its header for the liveness rules). It used to be an `INCR` counter inline here,
    # which never reclaimed: every seat that ever ran this recipe burned an index forever, so the
    # counter walked past the cap (68/73/74/80 seen in the wild) and refused every new seat, with
    # `just test-db-down` -- the one thing CLAUDE.md forbids mid-round -- as the only remedy on
    # offer. Allocation now takes the LOWEST FREE index, and `just test-db-release` /
    # `just test-db-reclaim` hand indices back without touching the shared containers.
    #
    # Keyed on the DERIVED `name` (not `raw_name`), so the registry, the Postgres pair above and
    # the exports below can never disagree about what this seat is called. Stdout is the index and
    # nothing else; the script's human-readable lines go to stderr and reach the terminal directly.
    redis_db="$(bash scripts/redis-seat-registry.sh allocate \
        --redis-container "$redis_container" \
        --seat "$name" \
        --capacity "$redis_databases" \
        --origin "$PWD")"
    echo ""
    echo "Export these before running pytest in this worktree:"
    echo "  export TEST_DATABASE_URL=\"postgresql+asyncpg://phaze:phaze@localhost:${port}/${main_db}\""
    echo "  export MIGRATIONS_TEST_DATABASE_URL=\"postgresql+asyncpg://phaze:phaze@localhost:${port}/${migrations_db}\""
    echo "  export PHAZE_REDIS_URL=\"redis://localhost:${redis_port}/${redis_db}\""
    echo ""
    echo "When this worktree is finished: just test-db-release {{name}}  (frees its Redis index; no teardown)"

[doc('Show who holds each Redis logical DB on the shared test harness, and which allocations look stale (read-only)')]
[group('test')]
test-db-seats:
    #!/usr/bin/env bash
    set -euo pipefail
    # phaze-68wky: the registry was invisible before this recipe -- the only way to see it was
    # `docker exec phaze-test-redis redis-cli HGETALL ...`, so an exhausted cap looked like a dead
    # end rather than a list of seats to hand back. Read-only: allocates nothing, frees nothing.
    bash scripts/redis-seat-registry.sh list \
        --redis-container "{{test_redis_container}}" \
        --pg-container "{{test_db_container}}" \
        --capacity "{{test_redis_databases}}"

[doc('Hand ONE seats Redis logical DB back to the shared test harness -- non-destructive, never touches the containers or any other seat')]
[group('test')]
test-db-release name *flags:
    #!/usr/bin/env bash
    set -euo pipefail
    # phaze-68wky: the non-destructive counterpart to `test-db-for`. Run it when a worktree is
    # finished. It clears that seat's OWN Redis keys and frees its index for the next seat; every
    # other seat, both containers, and every Postgres database are untouched. This is the remedy
    # `test-db-down` was being misused for. Refuses while a client is connected to the seat's Redis
    # DB or a backend is on its Postgres database (i.e. a suite is running in it) -- pass --force
    # after `just test-db-seats` shows you why, if you know better.
    name="$(bash scripts/derive-seat-name.sh "{{name}}")"
    echo "Seat '{{name}}' -> identifier '${name}'."
    bash scripts/redis-seat-registry.sh release \
        --redis-container "{{test_redis_container}}" \
        --pg-container "{{test_db_container}}" \
        --seat "$name" \
        --capacity "{{test_redis_databases}}" \
        {{flags}}
    echo ""
    echo "The Postgres databases for this seat were left in place (they hold no index and block nobody);"
    echo "re-running \`just test-db-for {{name}}\` reuses them and takes a fresh Redis index."

[doc('Sweep every Redis logical DB whose seat is no longer in use back into the pool -- dry run by default, --apply to free them; never touches the containers')]
[group('test')]
test-db-reclaim *flags:
    #!/usr/bin/env bash
    set -euo pipefail
    # phaze-68wky: this is the fix for "the registry is full and the only way out is test-db-down".
    # A seat is left alone if a Redis client is connected to its DB, if a Postgres backend is on its
    # database (pytest holds one for its whole session), or if its lease is still live; everything
    # else is freed. See scripts/redis-seat-registry.sh for the full rule set, and run
    # `just test-db-seats` first to see the evidence behind each verdict.
    bash scripts/redis-seat-registry.sh reclaim \
        --redis-container "{{test_redis_container}}" \
        --pg-container "{{test_db_container}}" \
        --capacity "{{test_redis_databases}}" \
        {{flags}}

[doc('Stop and remove the SHARED test-harness Postgres + Redis (phaze-test-db/phaze-test-redis) -- affects every concurrent worktree/session using them')]
[group('test')]
test-db-down:
    #!/usr/bin/env bash
    set -euo pipefail
    container="{{test_db_container}}"
    # phaze-ieqg: this removes containers EVERY concurrent worktree shares, and the doc comment
    # above has said so since phaze-20vd. A warning in `just --list` is read at leisure, not at
    # the moment somebody types the command. On 2026-07-29 18:17 UTC a `test-db-down` (+ implicit
    # recreate) mid-round destroyed 89 per-worktree databases AND the Redis DB-index allocation
    # registry while five full suites were in flight. Every one of those suites went red with
    # branch-unrelated failures that passed on isolated re-run -- the exact false-red signature
    # this bead was opened to explain, and hours of the round were spent triaging it as a code
    # regression. So: check for live seats instead of warning about them.
    #
    # A pytest session shows up here whether or not it is mid-query: `pytest_sessionstart` holds
    # an advisory-lock connection to its own database for the whole run (tests/db_guard.py), so an
    # idle-looking suite is still a visible `client backend` on a `phaze%` database. The pattern is
    # deliberately NOT narrowed to `phaze%test`: a perf DB misplaced on this shared container (e.g.
    # `phaze_perf82`, phaze-zpdyg) would otherwise be invisible to this guard even though it lives
    # on the exact container this recipe is about to remove.
    if [ "${PHAZE_TEST_DB_FORCE_DOWN:-}" != "1" ] && \
       [ "$(docker inspect -f '{{{{.State.Running}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
        busy="$(docker exec "$container" psql -U phaze -d postgres -tAc \
            "SELECT string_agg(DISTINCT datname || '  (backend pid ' || pid || ', ' || coalesce(nullif(application_name, ''), 'unnamed client') || ')', chr(10) || '     ')
               FROM pg_stat_activity
              WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() AND datname LIKE 'phaze%'" 2>/dev/null || true)"
        if [ -n "$(printf '%s' "$busy" | tr -d '[:space:]')" ]; then
            echo "❌ Refusing to remove the SHARED test harness: another seat is using it." >&2
            echo "     ${busy}" >&2
            echo "" >&2
            echo "   Removing ${container} now would delete every per-worktree database and the Redis" >&2
            echo "   DB-index registry out from under those runs. They would not fail cleanly -- they" >&2
            echo "   would report branch-unrelated failures that pass on isolated re-run, which reads" >&2
            echo "   as a code regression and costs a review round to disprove (phaze-ieqg)." >&2
            echo "" >&2
            echo "   If you came here to free Redis logical DBs, stop: \`just test-db-reclaim\` hands back" >&2
            echo "   every seat that is no longer in use without removing anything, and \`just test-db-release\`" >&2
            echo "   <name> frees a single one. Removing the harness for that was the phaze-68wky defect." >&2
            echo "" >&2
            echo "   Otherwise wait for those runs to finish, or PHAZE_TEST_DB_FORCE_DOWN=1 just test-db-down" >&2
            echo "   if you know the connections are stale." >&2
            exit 1
        fi
    fi
    docker rm -f "{{test_db_container}}" >/dev/null 2>&1 || true
    docker rm -f "{{test_redis_container}}" >/dev/null 2>&1 || true
    echo "🧹 Removed {{test_db_container}} + {{test_redis_container}}"

[doc('Stop and remove any leftover DEDICATED integration-test Postgres + Redis containers (matches the phaze-integration-test- name prefix, so it sweeps up every invocations containers; never the shared phaze-test-db/phaze-test-redis harness)')]
[group('test')]
integration-test-down:
    #!/usr/bin/env bash
    set -euo pipefail
    # phaze-987z: container names now carry a per-invocation unique suffix, so there is no
    # single fixed name left to `docker rm -f` -- sweep by name PREFIX instead. This is the
    # explicit cleanup path for anything an EXIT trap missed (e.g. a killed -9 shell).
    ids="$(docker ps -aq --filter "name=phaze-integration-test-" 2>/dev/null || true)"
    if [ -n "$ids" ]; then
        # shellcheck disable=SC2086  # $ids is a docker-generated, space-separated list of container IDs
        docker rm -f $ids >/dev/null 2>&1 || true
    fi
    echo "🧹 Removed any leftover phaze-integration-test-* containers"

[doc('Run the full suite against DEDICATED, disposable Postgres + Redis (auto teardown; phaze-pik6/phaze-987z -- per-invocation unique container names + dynamic ports so concurrent runs never race; never touches the SHARED phaze-test-db/phaze-test-redis harness other worktrees rely on)')]
[group('test')]
integration-test:
    #!/usr/bin/env bash
    set -euo pipefail
    # phaze-987z: a per-invocation unique token (this shell's PID + $RANDOM) so two concurrent
    # `integration-test` runs never share a container name -- the EXIT trap below then only
    # ever removes THIS invocation's own containers, never another run's.
    token="$$_${RANDOM}"
    container="{{integration_db_container_prefix}}-${token}"
    redis_container="{{integration_redis_container_prefix}}-${token}"
    fixed_db_port="{{integration_db_port}}"
    fixed_redis_port="{{integration_redis_port}}"
    trap 'docker rm -f "$container" "$redis_container" >/dev/null 2>&1 || true' EXIT
    if [ "$fixed_db_port" = "0" ]; then
        echo "🐘 Starting ${container} ({{postgres_image}}) on a dynamically-assigned host port..."
        docker run -d --name "$container" \
            -e POSTGRES_USER=phaze \
            -e POSTGRES_PASSWORD=phaze \
            -e POSTGRES_DB=phaze_test \
            --shm-size {{postgres_shm_size}} \
            -p 127.0.0.1::5432 \
            {{postgres_image}} >/dev/null
        port="$(docker port "$container" 5432/tcp | head -1 | sed -E 's/.*:([0-9]+)$/\1/')"
    else
        port="$fixed_db_port"
        echo "🐘 Starting ${container} ({{postgres_image}}) on host port ${port} (pinned via PHAZE_INTEGRATION_TEST_DB_PORT)..."
        docker run -d --name "$container" \
            -e POSTGRES_USER=phaze \
            -e POSTGRES_PASSWORD=phaze \
            -e POSTGRES_DB=phaze_test \
            --shm-size {{postgres_shm_size}} \
            -p "{{test_db_bind_ip}}:${port}:5432" \
            {{postgres_image}} >/dev/null
    fi
    if [ "$fixed_redis_port" = "0" ]; then
        echo "🟥 Starting ${redis_container} (redis:7-alpine) on a dynamically-assigned host port..."
        docker run -d --name "$redis_container" \
            -p 127.0.0.1::6379 \
            redis:7-alpine >/dev/null
        redis_port="$(docker port "$redis_container" 6379/tcp | head -1 | sed -E 's/.*:([0-9]+)$/\1/')"
    else
        redis_port="$fixed_redis_port"
        echo "🟥 Starting ${redis_container} (redis:7-alpine) on host port ${redis_port} (pinned via PHAZE_INTEGRATION_TEST_REDIS_PORT)..."
        docker run -d --name "$redis_container" \
            -p "{{test_db_bind_ip}}:${redis_port}:6379" \
            redis:7-alpine >/dev/null
    fi
    echo "⏳ Waiting for Postgres to accept connections..."
    for _ in $(seq 1 30); do
        if docker exec "$container" pg_isready -U phaze -d phaze_test >/dev/null 2>&1; then
            db_ready=1
            break
        fi
        sleep 1
    done
    if [ "${db_ready:-0}" != "1" ]; then
        echo "❌ ${container} did not become ready within 30s" >&2
        docker logs "$container" >&2 || true
        exit 1
    fi
    echo "⏳ Waiting for Redis to accept connections..."
    for _ in $(seq 1 30); do
        if docker exec "$redis_container" redis-cli ping >/dev/null 2>&1; then
            redis_ready=1
            break
        fi
        sleep 1
    done
    if [ "${redis_ready:-0}" != "1" ]; then
        echo "❌ ${redis_container} did not become ready within 30s" >&2
        docker logs "$redis_container" >&2 || true
        exit 1
    fi
    # phaze-hk8r: tolerate a lost create race -- see scripts/ensure-pg-database.sh's header.
    # This container is per-invocation-unique, so the race is only theoretical here, but the
    # ensure step stays consistent with the other two provisioning sites.
    bash scripts/ensure-pg-database.sh "$container" phaze_migrations_test
    export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:${port}/phaze_test"
    export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:${port}/phaze_migrations_test"
    export PHAZE_REDIS_URL="redis://localhost:${redis_port}/0"
    uv run pytest tests/ -q

[doc('Run ruff linter')]
[group('lint')]
lint:
    uv run ruff check .

[doc('Run ruff linter with auto-fix')]
[group('lint')]
lint-fix:
    uv run ruff check . --fix

[doc('Format code with ruff')]
[group('lint')]
fmt:
    uv run ruff format .

[doc('Run mypy type checker')]
[group('lint')]
typecheck:
    uv run mypy .

[doc('Run all pre-commit hooks')]
[group('lint')]
pre-commit:
    uv run pre-commit run --all-files

[doc('Run all quality checks (lint + typecheck + test); auto-provisions the ephemeral test-db when no TEST_DATABASE_URL override is already exported (e.g. a fresh worktree)')]
[group('lint')]
check: lint typecheck
    #!/usr/bin/env bash
    set -euo pipefail
    # A fresh worktree has no Postgres/Redis of its own -- `test` (bare `uv run
    # pytest`) then dies at fixture setup dialing tests/db_guard.py's resolve_test_dsn()
    # default (localhost:5433, the local ephemeral harness -- NOT the same as CI, which
    # always exports its own TEST_DATABASE_URL against its 5432 service container and
    # never relies on this default) with nothing listening there yet. `check` provisions
    # the SHARED test harness
    # (idempotently, via the existing `test-db` recipe) and exports the matching env
    # here, but never tears it down -- unlike `integration-test`, which runs against
    # its own DEDICATED containers with an auto-teardown EXIT trap (phaze-pik6),
    # `check` must leave phaze-test-db/phaze-test-redis running for other concurrent
    # worktrees/sessions relying on them; explicit teardown is `just test-db-down`. If
    # the caller already exported TEST_DATABASE_URL (CI, another `just` recipe, a
    # shared multi-worktree rig with per-worktree database names), respect it
    # verbatim and skip provisioning.
    if [ -z "${TEST_DATABASE_URL:-}" ]; then
        just test-db
        export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:{{test_db_port}}/phaze_test"
        export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:{{test_db_port}}/phaze_migrations_test"
        export PHAZE_REDIS_URL="redis://localhost:{{test_redis_port}}/0"
    fi
    just test

[doc('Run pip-audit for dependency vulnerability scanning')]
[group('security')]
pip-audit:
    #!/usr/bin/env bash
    set -e
    IGNORE_ARGS=""
    if [[ -f .pip-audit-ignores ]]; then
        while IFS= read -r line; do
            vuln_id=$(echo "$line" | sed 's/#.*//' | tr -d '[:space:]')
            [[ -z "$vuln_id" ]] && continue
            IGNORE_ARGS="$IGNORE_ARGS --ignore-vuln $vuln_id"
        done < .pip-audit-ignores
    fi
    # shellcheck disable=SC2086
    uv run pip-audit --desc --skip-editable $IGNORE_ARGS

[doc('Run bandit for Python SAST')]
[group('security')]
security:
    uv run bandit -r src/ -x tests -s B608

[doc('Run all security checks')]
[group('security')]
security-all: pip-audit security

[doc('View worker logs (follow mode)')]
[group('worker')]
worker-logs:
    docker compose logs -f worker

[doc('Restart worker service')]
[group('worker')]
worker-restart:
    docker compose restart worker

[doc('Check SAQ worker health')]
[group('worker')]
worker-health:
    docker compose exec worker uv run saq phaze.tasks.controller.settings --check

[doc('Build Docker images')]
[group('docker')]
docker-build:
    docker compose build

[doc('Validate Dockerfiles with hadolint')]
[group('docker')]
docker-validate:
    #!/usr/bin/env bash
    set -e
    for df in Dockerfile; do
        echo "🔍 Validating ${df}..."
        docker run --rm -i hadolint/hadolint < "${df}"
        echo "✅ ${df} passed"
    done

[doc('Push Docker images to GHCR (requires: gh auth token with packages:write)')]
[group('docker')]
image-push:
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    TAG="latest"
    declare -A IMAGES=(
        ["api"]="Dockerfile"
    )
    # Matches docker-publish.yml's image_suffix matrix (Phase 29 D-15): the api image
    # publishes BARE (ghcr.io/<owner>/<repo>:<tag>, no sub-path -- docker-compose.agent.yml's
    # watcher/worker services pull exactly that reference).
    declare -A IMAGE_SUFFIX=(
        ["api"]=""
    )
    for SERVICE in "${!IMAGES[@]}"; do
        IMAGE="${REGISTRY}/${OWNER}/${REPO}${IMAGE_SUFFIX[$SERVICE]}:${TAG}"
        echo "🐳 Building and pushing ${IMAGE}..."
        docker build -f "${IMAGES[$SERVICE]}" -t "${IMAGE}" .
        docker push "${IMAGE}"
        echo "✅ ${SERVICE} pushed"
    done

[doc('Build the arm64 essentia agent image locally (operator fallback to the CI build-arm64 job)')]
[group('docker')]
image-build-arm64 TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}-arm64"
    echo "🐳 Building ${IMAGE} (Dockerfile.agent-arm64, native arm64 essentia)..."
    docker build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 -t "${IMAGE}" .
    echo "✅ built ${IMAGE}"

[doc('Build + push the arm64 essentia agent image to GHCR (operator fallback; CI push is parity-gated in 47-04)')]
[group('docker')]
image-push-arm64 TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}-arm64"
    echo "🐳 Building and pushing ${IMAGE}..."
    docker build --build-arg TF_VERSION=2.20.0 -f Dockerfile.agent-arm64 -t "${IMAGE}" .
    docker push "${IMAGE}"
    echo "✅ ${IMAGE} pushed"

[doc('Regenerate the x86 parity golden JSON from the reference clip (operator path; CI in plan 47-04 is authoritative)')]
[group('docker')]
parity-golden-regen TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    # CI publishes the api image at the bare-repo URL (image_suffix="" for api,
    # Phase 29 D-15) — ghcr.io/<owner>/<repo>:<tag>, NOT a /api sub-path. Match it.
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}"
    # 1. Provision the essentia model weights locally (host ./models).
    echo "📥 Provisioning models into ./models ..."
    bash scripts/download-models.sh models
    # 2. Run the SHARED dump tool inside the x86 api image over the committed reference clip.
    #    This writes scripts/parity/golden-x86.json for offline inspection.
    #    NOTE: CI (plan 47-04) is the AUTHORITATIVE golden producer; this is the operator regen path.
    echo "🐳 Generating golden-x86.json via ${IMAGE} ..."
    # The image runs as a non-root user that cannot write into the host-owned
    # bind-mounted scripts/parity dir; write into a world-writable output dir and
    # copy the result out host-side (same fix as the parity-dump recipe).
    OUT_DIR=$(mktemp -d)
    chmod 777 "${OUT_DIR}"
    docker run --rm \
        -v "$(pwd)/scripts/parity:/parity:ro" \
        -v "$(pwd)/models:/models:ro" \
        -v "${OUT_DIR}:/out" \
        "${IMAGE}" \
        uv run python /parity/dump_analysis.py /parity/reference.wav /models --out /out/golden-x86.json
    cp "${OUT_DIR}/golden-x86.json" scripts/parity/golden-x86.json
    rm -rf "${OUT_DIR}"
    echo "✅ wrote scripts/parity/golden-x86.json"

[doc('Run the shared analyze_file dump inside an image; INTERP picks "uv run python" (x86 uv image) vs python3 (arm64 --system 3.13 agent image)')]
[group('docker')]
parity-dump IMAGE MODELS="./models" OUT="scripts/parity/actual.json" INTERP="uv run python":
    #!/usr/bin/env bash
    set -e
    # The SHARED dump path BOTH CI parity jobs delegate to (workflows delegate to
    # just — MEMORY). INTERP selects the in-image interpreter: the x86 api image
    # runs the uv-managed venv (default "uv run python"); the arm64 agent image
    # installs --system on 3.13 and MUST run python3 directly (uv run would
    # re-validate requires-python >=3.14 and miss the --system packages).
    OUT_BASE=$(basename "{{OUT}}")
    # The image runs as a NON-ROOT user that cannot write into the host-owned
    # bind-mounted scripts/ dir (PermissionError on /scripts/parity/<out>.json).
    # Mount scripts read-only and give the container a dedicated world-writable
    # output dir to write --out into, then copy the result to {{OUT}} host-side.
    OUT_DIR=$(mktemp -d)
    chmod 777 "${OUT_DIR}"
    echo "🐳 Dumping analyze_file from {{IMAGE}} (interp: {{INTERP}}) → {{OUT}} ..."
    docker run --rm \
        -v "$(pwd)/scripts:/scripts:ro" \
        -v "$(pwd)/{{MODELS}}:/models:ro" \
        -v "${OUT_DIR}:/out" \
        "{{IMAGE}}" \
        {{INTERP}} /scripts/parity/dump_analysis.py /scripts/parity/reference.wav /models --out "/out/${OUT_BASE}"
    cp "${OUT_DIR}/${OUT_BASE}" "{{OUT}}"
    rm -rf "${OUT_DIR}"
    echo "✅ wrote {{OUT}}"

[doc('Run the arm64↔x86 numeric parity check locally (operator mirror of the CI parity-guard)')]
[group('docker')]
parity-check TAG="latest":
    #!/usr/bin/env bash
    set -e
    REGISTRY="ghcr.io"
    OWNER=$(echo "$(git remote get-url origin)" | sed 's|.*github.com[:/]||;s|/.*||' | tr '[:upper:]' '[:lower:]')
    REPO=$(basename -s .git "$(git remote get-url origin)" | tr '[:upper:]' '[:lower:]')
    IMAGE="${REGISTRY}/${OWNER}/${REPO}:{{TAG}}-arm64"
    # 1. Provision the essentia model weights locally (host ./models).
    echo "📥 Provisioning models into ./models ..."
    bash scripts/download-models.sh models
    # 2. Dump the arm64 actual via the shared recipe — direct python3 for the agent image.
    just parity-dump "${IMAGE}" ./models scripts/parity/actual.json python3
    # 3. Compare against the committed/CI golden (non-zero exit on any parity break).
    echo "🔬 Comparing scripts/parity/actual.json against scripts/parity/golden-x86.json ..."
    uv run python scripts/parity/compare_analysis.py scripts/parity/golden-x86.json scripts/parity/actual.json

[doc('Validate docker-compose.yml syntax')]
[group('docker')]
docker-compose-validate:
    docker compose config --quiet && echo "✅ docker-compose.yml is valid"

[doc('Shell into the API container')]
[group('docker')]
docker-shell:
    docker compose exec api bash

[doc('View running containers')]
[group('docker')]
docker-ps:
    docker compose ps

[doc('Run Alembic migrations')]
[group('db')]
db-upgrade:
    uv run alembic upgrade head

[doc('Create a new Alembic migration')]
[group('db')]
db-revision MESSAGE:
    uv run alembic revision --autogenerate -m "{{MESSAGE}}"

[doc('Show current migration status')]
[group('db')]
db-current:
    uv run alembic current

[doc('Downgrade one migration')]
[group('db')]
db-downgrade:
    uv run alembic downgrade -1

[doc('Show migration history')]
[group('db')]
db-history:
    uv run alembic history

[doc('Start a DEDICATED ephemeral Postgres for the PERF-02 bench (own port, never wiped by test-db recreates)')]
[group('db')]
perf-db-up:
    #!/usr/bin/env bash
    set -euo pipefail
    container="{{perf_db_container}}"
    port="{{perf_db_port}}"
    # phaze-uame5: mirror test-db's `docker start`-first pattern (phaze-20vd). This container
    # is the durable home for the ~200K-row PERF-02 corpus (see the recipe doc comment above),
    # seeded into its writable layer with no volume backing it. `docker run` has no --restart
    # flag, so the normal state after a host reboot or daemon restart is "exists, stopped" --
    # the previous `docker rm -f` on that path destroyed the corpus and silently reprovisioned
    # an empty database, printing the same "Starting..." line either way. `docker start`
    # succeeds on a stopped container and fails harmlessly when none exists, so no `rm -f` is
    # needed here at all -- and skipping it also avoids reintroducing the speculative-rm race
    # phaze-20vd eliminated from test-db (a concurrent `just perf-db-up` racing our own
    # `docker run` could otherwise have its just-created container deleted out from under it).
    run_or_yield() {
        local run_err
        run_err="$(mktemp)"
        if docker run -d --name "$container" \
            -e POSTGRES_USER=phaze -e POSTGRES_PASSWORD=phaze -e POSTGRES_DB={{perf_db_name}} \
            --shm-size {{postgres_shm_size}} \
            -p "{{test_db_bind_ip}}:${port}:5432" {{postgres_image}} >/dev/null 2>"$run_err"; then
            rm -f "$run_err"
            return 0
        fi
        if grep -q "is already in use" "$run_err"; then
            echo "🔁 ${container} was created by a concurrent invocation; continuing"
            rm -f "$run_err"
            return 0
        fi
        cat "$run_err" >&2
        rm -f "$run_err"
        return 1
    }
    if [ "$(docker inspect -f '{{{{.State.Running}}' "$container" 2>/dev/null || echo false)" = "true" ]; then
        echo "🐘 ${container} already running on port ${port}"
    else
        echo "🐘 Starting ${container} ({{postgres_image}}) on host port ${port}..."
        if ! docker start "$container" >/dev/null 2>&1; then
            run_or_yield
        fi
    fi
    for _ in $(seq 1 30); do
        if docker exec "$container" pg_isready -U phaze -d {{perf_db_name}} >/dev/null 2>&1; then
            echo "✅ ${container} ready on localhost:${port} ({{perf_db_name}})"; exit 0
        fi
        sleep 1
    done
    echo "❌ ${container} did not become ready within 30s" >&2; exit 1

[doc('Stop and remove the dedicated PERF-02 bench Postgres')]
[group('db')]
perf-db-down:
    docker rm -f "{{perf_db_container}}" >/dev/null 2>&1 || true
    @echo "🧹 Removed {{perf_db_container}}"

[doc('Migrate the perf DB to HEAD (>=036) and seed the ~N synthetic corpus for the PERF-02 bench (Phase 82)')]
[group('db')]
perf-seed N='200000':
    PHAZE_DATABASE_URL="{{perf_db_sa_dsn}}" uv run alembic upgrade head
    uv run python scripts/seed_perf_corpus.py --n {{N}} --dsn "{{perf_db_dsn}}" --reseed

[doc('EXPLAIN ANALYZE the derived hot queries + time /pipeline/stats against the seeded perf DB (PERF-02, D-07)')]
[group('db')]
perf-explain ITER='20':
    uv run python scripts/perf_explain.py --dsn "{{perf_db_dsn}}" --iterations {{ITER}}

[doc('Download essentia ML models for audio analysis')]
[group('models')]
download-models:
    bash scripts/download-models.sh models

[doc('Update pre-commit hooks (with frozen SHAs)')]
[group('maintenance')]
update-hooks:
    uv run pre-commit autoupdate --freeze

[doc('Lock and upgrade all dependencies')]
[group('maintenance')]
lock-upgrade:
    uv lock --upgrade

[doc('Sync after lock upgrade')]
[group('maintenance')]
sync:
    uv sync
