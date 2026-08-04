# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**phaze** — A music alignment tool. Python 3.14, MIT licensed.

## Development Setup

- **Python**: 3.14 exclusively
- **Package manager**: `uv` only — never use bare `pip`, `python`, `pytest`, or `mypy`. Always prefix with `uv run`.
- **Pre-commit**: Must be installed and active. All hooks must pass before commits.

### Key Commands

```bash
uv sync                    # Install dependencies
uv run pytest              # Run tests
uv run pytest tests/test_foo.py::test_bar  # Run a single test
uv run pytest --cov --cov-report=term-missing  # Run tests with coverage
uv run ruff check .        # Lint
uv run ruff format .       # Format
uv run mypy .              # Type check
uv run pre-commit run --all-files # Run all pre-commit hooks

just check                 # Lint + typecheck + the full test suite (the hive validation)
just test-db               # Bring up the shared test Postgres (5433) + Redis (6380) harness
just test-db-for <name>    # Carve an isolated seat out of that harness — REQUIRED for
                           # concurrent worktrees; prints three exports to set
```

> Bare `uv run pytest` needs the harness up (`just test-db`); without it the integration tests
> skip, so a green run means less than it looks like. Check the database line in the pytest
> header before trusting any result.

### Test databases

The test suite resolves its target from `TEST_DATABASE_URL`, validated by a single guard in
`tests/db_guard.py`. Two rules, both enforced:

- **The database name must contain a `test` segment** — `phaze_test`, `phaze_test_<bead>`, and
  `phaze_<bead>_test` are all accepted; `phaze` and `phaze_prod` are not. A name that fails this
  check **errors the run**. It does not skip. A skip would silently drop ~18 integration tests
  while pytest still reported green, which is exactly the defect this guard replaced.
- **Port 5433, never 5432.** 5433 is the ephemeral test harness (`just test-db`); 5432 is
  reserved for the developer's own database. The fixtures create and drop schema, so a default
  pointing at 5432 is a live-data-loss shape, not just a confusing error. An unset
  `TEST_DATABASE_URL` defaults to `postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test`,
  so the bare `uv run pytest` above stays safe and needs no extra setup.

Every run prints its resolved target in the pytest header
(`phaze test database: 'phaze_test' on localhost:5433 (from TEST_DATABASE_URL, exclusive)`) — check
it before trusting a green run. `exclusive` means this pytest process holds the session lock
described below. The header renders the other state as the literal
`unlocked (Postgres unreachable or bypass set)`, and it has **three** causes, not one:

1. **Postgres was unreachable** at session start (no harness up) — the lock could not be taken.
2. **`PHAZE_TEST_DB_ALLOW_SHARED=1`** was set, deliberately bypassing the guard.
3. **`--collect-only`**, which is exempt: it imports modules and never opens the schema, so it
   cannot corrupt a live run and stays usable for inspecting a suite *while* it runs.

In cases 1 and 2 the run is *not* protected and its failures are not trustworthy under any
concurrency.

**Never share Postgres OR Redis between concurrent agents.** Both are stateful, both are shared by
default, and both must be isolated per worktree. Saying "test database" here was the phaze-fwo7
defect: it taught agents to isolate Postgres and left every seat on the same logical Redis.

```bash
just test-db-for <name>    # derives <derived> from <name> (see below), creates
                           # phaze_<derived>_test + phaze_<derived>_migrations_test,
                           # allocates a dedicated Redis logical DB, and prints all three exports
```

`<name>` is not used verbatim (phaze-fmfk): `test-db-for` normalizes it into `<derived>` by
turning hyphens into underscores and appending a short hash of the original `<name>`, e.g.
`review-polite` → `phaze_review_polite_7a21035a_test`. The hash exists so that `my-seat` and
`my_seat` — which normalize to identical text — don't silently collide onto one shared seat, the
same class of defect described in "Why Redis matters" below; do not "simplify" the recipe back to
a bare `phaze_<name>_test` substitution. **Always copy the exports the recipe prints** rather
than hand-constructing the DSN from `<name>` yourself — the two agree only when `<name>` has no
hyphens.

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<derived>_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<derived>_migrations_test"
export PHAZE_REDIS_URL="redis://localhost:6380/<index>"
```

**Why Redis matters as much as Postgres.** Two redis-backed test modules
(`tests/review/routers/test_execution_dispatch.py` and `test_agent_exec_batches.py`) run a global
`scan_iter`+`delete` sweep over `exec:*`, `exec_progress_req:*` and `execdispatch:*` in fixture
setup *and* teardown. On a shared logical database one agent's fixture deletes another agent's live
keys mid-test, and assertions that count the keyspace see foreign keys. The result is a failure
indistinguishable from a real regression that passes on isolated re-run — the worst possible shape,
because it trains reviewers to dismiss red runs.

Redis DB indices are allocated from an atomic registry on the test container (DB 0 holds the
registry; seats get 1 upward), so re-running `test-db-for` for the same worktree is idempotent and
two worktrees can never collide. The container is started with 64 logical databases; allocation
past that fails loudly rather than wrapping onto a shared index. **Leaving `PHAZE_REDIS_URL` unset
is still valid for single-agent and CI runs** — it defaults to DB 0.

### One database, one pytest process (phaze-ieqg)

`TEST_DATABASE_URL` isolates a **worktree**. It never isolated a **process**, and that gap — not
some undiscovered third shared surface — is what made full-suite runs untrustworthy under
concurrency for two dispatch rounds.

Two pytest processes on one database destroy each other. `tests/conftest.py`'s session-scoped
`async_engine` runs `Base.metadata.create_all` at session start and `drop_all` at session teardown,
so whichever process finishes **first** drops the schema out from under the other. Measured:
`pytest tests/analyze/routers` (61 tests, 6.8 s) and `pytest tests/review` sharing one database left
the second run at **238 failed + 12 errors**, all `UndefinedTableError: relation "agents" does not
exist`, all green on isolated re-run. The most common way to hit this is the most natural one:
re-running a subset "to check something in isolation" in a second terminal while the full suite is
still going, or a reviewer running the suite in the same worktree the developer is working in.

`pytest_sessionstart` now takes a session-level Postgres advisory lock on the resolved database and
holds it for the whole run. A second process is **refused before collection** with the holder's pid
and the fix, instead of silently corrupting both runs. `PHAZE_TEST_DB_ALLOW_SHARED=1` bypasses it;
pytest-xdist against one database is this exact defect and is not a reason to set it (CI keeps every
DB bucket serial for the same reason).

Two suites in two worktrees with their own `test-db-for` databases are unaffected — that is the
supported way to run concurrently, and it is verified green.

### `pg_locks` and `pg_stat_activity` are cluster-wide — always scope them

A per-worktree database isolates table data completely and the system catalogues not at all. Any
test that reads `pg_locks` or `pg_stat_activity` sees **every** seat's backends. Two concurrent
suites, each correctly isolated, both went red on
`tests/integration/test_tag_bulk_write_advisory_lock.py` with `assert 2 == 1` — an advisory-lock
count that had picked up the other seat's copy of the same application key. The three
`_wait_for_blocked_waiter` barriers had the nastier version: `SELECT EXISTS (SELECT 1 FROM pg_locks
WHERE NOT granted)` is satisfied by any blocked backend in the cluster, so the barrier returned
before the test's own waiter had queued and everything after it raced.

Scope every such query with `current_database()`. For an advisory-lock count use
`and database = (select oid from pg_database where datname = current_database())`; for a
"somebody is blocked" barrier join the waiting backend instead
(`pg_locks.database` is NULL for `transactionid` locks, so the column filter never matches there) —
`tests/db_guard.BLOCKED_WAITER_SQL` is the shared correct form.
`tests/shared/test_cluster_wide_catalog_scoping.py` fails the build on an unscoped query.

### Never `just test-db-down` while another seat is running

`phaze-test-db` and `phaze-test-redis` are **one shared pair of containers**; `test-db-for` carves
seats out of them rather than giving each seat its own. On 2026-07-29 a `test-db-down` + recreate
mid-round destroyed 89 per-worktree databases and reset the Redis allocation registry while five
suites were in flight, producing the same false-red signature from a different cause. `test-db-down`
now refuses while any client is connected to a `phaze%test` database, listing the seats it is
protecting; `PHAZE_TEST_DB_FORCE_DOWN=1` overrides for genuinely stale connections.

## Code Quality

### Ruff Configuration

Line length: 150. Ruff lint `target-version` is `py313` — intentionally one minor behind the 3.14 runtime. Python 3.14's PEP 649 deferred annotations make ruff's `TC`/`UP037` rewrites want to move type-only imports into `TYPE_CHECKING` blocks and unquote annotations, which breaks Pydantic/SQLAlchemy/FastAPI (they resolve annotations at runtime via `get_type_hints`). Keep `py313` until those rewrites are safe.

**Enabled rule sets**: `ARG`, `B`, `C4`, `E`, `F`, `I`, `PLC`, `PTH`, `RUF`, `S`, `SIM`, `T20`, `TCH`, `UP`, `W`, `W191`

**Ignored rules**: `B008`, `C901`, `E501`, `S101`

**Per-file ignores**: `__init__.py` ignores `F401`. `T201` (print) is allowed in `scripts/parity/**`, `src/phaze/cli/**`, `src/phaze/main.py`, and tests. `services/**` ignores `S603`/`S607`. Tests (`tests/**`) also ignore `PLC`, `S105`, and `ARG001`.

**isort**: `lines-after-imports = 2`, `combine-as-imports = true`, `split-on-trailing-comma = true`, `force-sort-within-sections = true`. Set `known-first-party` to project package name.

**Format**: `quote-style = "double"`, `indent-style = "space"`, `docstring-code-format = false`.

### Mypy Configuration

```toml
[tool.mypy]
python_version = "3.14"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
check_untyped_defs = true
disallow_untyped_decorators = true
no_implicit_optional = true
warn_redundant_casts = true
warn_unused_ignores = true
warn_no_return = true
warn_unreachable = true
strict_equality = true
mypy_path = ["src"]
exclude = "^(tests/|prototype/|services/|vulture_whitelist\\.py)"
```

Tests are excluded entirely (see `exclude` above), not run under a relaxed override.

### Pre-commit Hooks

Use frozen SHAs (not just tags) for all hooks. Required hooks:

- **pre-commit-hooks**: large files, executable shebangs, merge conflicts, TOML, YAML, JSON (check + pretty-format), AWS credentials, private keys, EOF fixer, trailing whitespace, mixed line endings
- **ruff-pre-commit**: `ruff --fix` + `ruff-format`
- **bandit**: `-x tests,services -s B608`
- **check-jsonschema**: GitHub workflows/actions validation
- **hadolint**: Dockerfile linting
- **actionlint**: GitHub Actions linting
- **yamllint**: strict mode
- **shellcheck-py**: `--shell=bash --severity=warning`
- **pre-commit-shfmt**: `--indent=2 --case-indent --language-dialect=bash --write`
- **Local mypy hook**: `uv run mypy .` with `pass_filenames: false`

## Testing

- Minimum **95% code coverage** required
- Upload coverage to Codecov with service-specific flags
- Codecov config: precision 2, round down, range 70-100%, project target auto with 1% threshold, patch target 80% with 5% threshold

## Workflow: Features and PRs

- **Every feature gets its own git worktree** — no cross-contamination between features
- **Every feature gets its own PR** — one PR per feature, no mixing unrelated changes
- Never push directly to main

## CI (GitHub Actions)

Follow the discogsography pattern:

- **Reusable workflows** via `workflow_call` — separate jobs for code quality, tests, security
- **Code quality job**: runs all pre-commit hooks
- **Test job**: runs pytest with coverage, uploads to Codecov with flags and `disable_search: true`
- **Security job**: pip-audit, bandit, osv-scanner, Semgrep, TruffleHog secret scanning, Trivy container scanning
- **Concurrency groups** with `cancel-in-progress` on PR workflows
- Emoji prefixes on all step names

## Code Style

- 150-character line length
- Type hints on all functions
- Double quotes for strings
- PEP 8 conventions
- `pyproject.toml` section order: `[build-system]` → `[project]` → `[project.scripts]` → `[tool.*]` → `[dependency-groups]`, with alphabetically sorted dependencies

<!-- GSD:project-start source:PROJECT.md -->
## Project

**Phaze**

A music collection organizer that ingests music files (mp3, m4a, ogg) and concert video streams, analyzes them, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

**Core Value:** Get messy music and concert files properly named, organized into logical folders, deduplicated, with rich metadata in Postgres — and provide a human-in-the-loop approval workflow so nothing moves without review.

### Constraints

- **Language**: Python 3.14 exclusively
- **Package manager**: uv only
- **Deployment**: Docker Compose on home server, private network
- **Database**: PostgreSQL
- **Scale**: Must handle large file counts efficiently — batch processing and parallelization required
- **Existing code**: Must integrate with provided analysis prototypes and respect their per-file interface
- **Naming format**: AI filename proposals — specific format TBD (will be provided later)
<!-- GSD:project-end -->

<!-- GSD:stack-start source:research/STACK.md -->
## Technology Stack

## Recommended Stack
### Core Technologies
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.14 | Runtime | Project constraint. essentia-tensorflow dev1438+ ships cp314 wheels only, requiring Python 3.14. |
| FastAPI | >=0.139.0 | Web framework / API | De facto standard for async Python APIs. Native async, auto-generated OpenAPI docs, Pydantic integration, SSE support for real-time UI updates. Massive ecosystem and community. |
| SQLAlchemy | >=2.0.51 | ORM / database toolkit | Industry standard Python ORM. Full async support via `create_async_engine` + asyncpg driver. Declarative models, relationship management, migration support via Alembic. |
| asyncpg | >=0.31.0 | PostgreSQL async driver | Fastest Python PostgreSQL driver. Purpose-built for asyncio. Used as SQLAlchemy's async backend. |
| Alembic | >=1.18.5 | Database migrations | Official SQLAlchemy migration tool. Async template support (`alembic init -t async`). Autogenerate from model changes. |
| PostgreSQL | 16+ (pinned to `postgres:18-alpine` in docker-compose/CI) | Primary database | Project constraint. Handles large-scale file metadata, complex queries, JSON columns for flexible metadata, full-text search for future features. |
| Redis | 8.x in prod (`redis:8-alpine`), client pinned `redis>=8.0.1,<9.0` | Cache / pub-sub | No longer the SAQ broker (Phase 36 migrated the task queue to Postgres); used for caching analysis results and rate-limiting LLM API calls. **The test harness runs `redis:7-alpine`** (`justfile:352,358`) while production runs `redis:8-alpine` (`docker-compose.yml:143`) — a version-skew gap the suite does not cover. |
| Docker Compose | 2.x | Deployment orchestration | Project constraint. Runs PostgreSQL, Redis, API server, worker processes as separate containers. |
### Audio / Music Libraries
| Library | Version | Purpose | Why Recommended |
|---------|---------|---------|-----------------|
| mutagen | >=1.48.1 | Audio metadata read/write | The standard for audio tag manipulation in Python. Supports ID3v1/v2, Vorbis, MP4, FLAC, OGG, AIFF. Zero dependencies. Read AND write capability needed for renaming workflows. |
| essentia-tensorflow | >=2.1b6.dev1438 | Audio feature extraction (BPM, key, mood, style) | Comprehensive MIR library with pre-trained TensorFlow models. Beat tracking, tempo estimation, key detection, mood/style classification. Used for all audio analysis in the main application. |
| pyacoustid | *(not a pyproject.toml dependency — never used)* | N/A — historical | Originally recommended for Chromaprint/AcoustID bindings. The audio-fingerprinting feature it would have served (the `audfprint`/Panako pipeline) was implemented independently of pyacoustid and removed from the product entirely 2026-07-28 (epic phaze-0jpe; see `docs/design/0002-fingerprint-removal.md`). pyacoustid remains unused. |
| chromaprint (system) | latest | retained permanently — no known consumer | C library (`libchromaprint`) kept in the app/agent images through the phaze-0jpe removal. **Correction (phaze-0jpe.6):** it was previously described here as an essentia-tensorflow runtime requirement; that was tested against the live deployment and found false — `ldd` on the deployed `_essentia` extension shows no chromaprint link, and `import essentia` succeeds without it. No `phaze` source calls `fpcalc`/`chromaprint`/`Chromaprinter`/`acoustid`. It plausibly dates from the original `pyacoustid`/AcoustID plan that was never implemented. **Operator decision 2026-07-29: KEEP permanently** — the open phaze-0jpe.6 question is closed as "keep". A runtime `dlopen` path was never exhaustively ruled out and the install cost is trivial, so retention is deliberate, not deferred; do not re-open it as a cleanup task. See `docs/design/0002-fingerprint-removal.md`. Provides the `fpcalc` binary. |
| FFmpeg (system) | 8.x | Audio/video processing | Required for audio decoding and video stream metadata extraction via ffprobe. Install in Docker image. |
### Web UI
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Jinja2 | >=3.1 | Server-side templating | Ships with FastAPI. Server-rendered HTML means no separate frontend build, no SPA complexity. Perfect for admin-only tool. |
| HTMX | 2.x (CDN) | Dynamic UI interactions | Eliminates need for React/Vue/Angular. Adds SPA-like interactivity (approve/reject buttons, live search, pagination) via HTML attributes. Zero build step. 90% of SPA functionality, 10% of complexity. |
| Tailwind CSS | 4.x (standalone binary, pinned in `justfile`) | Styling | Utility-first CSS. Compiled at image-build time by the pinned standalone Tailwind binary (`just tailwind`) — no Node, no CDN, no client-side compiler. DaisyUI component library optional for pre-built components. |
| Alpine.js | 3.x (CDN) | Lightweight JS interactions | 3KB library for dropdown menus, modals, toggling states. Complements HTMX for client-side state that HTMX doesn't handle. |
### Task Processing
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| SAQ | >=0.26.4 (`saq[postgres]`) | Async task queue | Purpose-built for asyncio. Inspired by arq with active maintenance. Broker migrated from Redis to Postgres in Phase 36 (`PostgresQueue`, `saq_jobs` table). Perfect for file analysis jobs (BPM, metadata extraction). Supports retries with backoff, job results, cron jobs, built-in web UI. Single-user app doesn't need Celery's complexity. |
### AI / LLM Integration
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| litellm | >=1.85.7,<1.86.0 (pin exact minor) | Unified LLM API client | Single interface to 100+ LLM providers (OpenAI, Anthropic, local models). Avoids vendor lock-in. Use for filename/path proposals. **IMPORTANT:** Pin exact minor line due to the March 2026 supply chain incident on versions 1.82.7-1.82.8. Verify checksums. |
| pydantic | >=2.10 | Data validation / LLM structured output | Already a FastAPI dependency. Use for validating LLM responses (proposed filenames, paths). Structured output parsing. |
### Configuration / Infrastructure
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| pydantic-settings | >=2.14.2 | Configuration management | Type-safe config from env vars, .env files, Docker secrets. Native Pydantic integration. Supports `SecretStr` for API keys. |
| uvicorn | >=0.51.0 | ASGI server | Standard production server for FastAPI. Use with `--workers` for multi-process or behind gunicorn for production. |
### Development Tools
| Tool | Purpose | Notes |
|------|---------|-------|
| uv | Package management | Project constraint. Fast, deterministic. Use `uv run` prefix for all commands. |
| ruff | Linting + formatting | Already configured in CLAUDE.md. Replaces flake8, black, isort. |
| mypy | Type checking | Already configured. Strict mode excluding tests. |
| pytest | Testing | With pytest-asyncio for async tests, pytest-cov for coverage. |
| pytest-asyncio | Async test support | Required for testing async endpoints, database operations, task queue jobs. |
| httpx | HTTP test client | FastAPI's recommended test client. Use `AsyncClient` for async endpoint testing. |
| pre-commit | Git hooks | Already configured in CLAUDE.md. |
## Installation
# Core application
# Audio processing
# AI integration
# Dev dependencies
# System dependencies (Dockerfile)
# apt-get install -y ffmpeg chromaprint-tools
## Alternatives Considered
| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| FastAPI | Litestar | If you want more explicit DI and slightly lower memory usage. FastAPI wins on ecosystem size, docs quality, and community support. |
| SQLAlchemy | SQLModel | If models are simple and you want less boilerplate. SQLModel is a thin FastAPI-aligned wrapper over SQLAlchemy but has fewer features and weaker async story. Stick with SQLAlchemy for large-scale systems. |
| SAQ | Celery | If you need multi-broker support, complex routing, or canvas workflows. Overkill for a single-user app. Celery's config complexity is not justified here. |
| SAQ | Dramatiq | If you want RabbitMQ support or more mature retry/middleware. Dramatiq is sync-first which conflicts with our async stack. |
| HTMX + Jinja2 | React/Vue SPA | If you need offline capability, complex client-side state, or multiple developers on frontend. A single-user admin tool does not need SPA complexity or a separate build pipeline. |
| litellm | Direct OpenAI SDK | If you are committed to a single LLM provider forever. litellm provides flexibility to switch between local/cloud models with zero code changes. |
| mutagen | tinytag | If you only need read-only metadata. We need write capability to update tags after renaming, so mutagen is required. |
| essentia-tensorflow | librosa | If you only need basic BPM/tempo and don't need pre-trained classification models. Essentia provides richer analysis (mood, style, danceability) via TensorFlow models. |
## What NOT to Use
| Avoid | Why | Use Instead |
|-------|-----|-------------|
| ffmpeg-python (pip: `ffmpeg-python`) | Last PyPI release was 2022. Effectively abandoned. 500+ open issues on GitHub. | Use `subprocess.run(["ffprobe", ...])` directly for metadata extraction. Or `python-ffmpeg` (pip: `python-ffmpeg`) which is actively maintained. |
| SQLite | Cannot handle concurrent writes from multiple worker processes analyzing files in parallel. No JSON operators for flexible metadata queries. | PostgreSQL (project constraint). |
| Celery | Massive dependency tree, complex configuration, sync-first design. Overkill for single-user app with Redis already in stack. | SAQ for async task queue. |
| Django | Full MVC framework with ORM, admin, auth -- all unnecessary when you have FastAPI + SQLAlchemy + custom admin UI. Sync-first design conflicts with async processing needs. | FastAPI. |
| LangChain | Enormous abstraction layer for LLM calls. This project just needs "send prompt, get structured response." LangChain adds complexity without benefit for simple classification/naming tasks. | litellm for provider abstraction + raw Pydantic for structured output. |
| React/Next.js | Requires separate build pipeline, Node.js in Docker, npm dependencies. Completely unnecessary for a single-user admin approval UI. | HTMX + Jinja2 + Tailwind CSS via CDN. |
| tinytag | Read-only metadata extraction. Cannot write updated tags back to files after renaming. | mutagen for read+write. |
| psycopg2 | Sync driver. Blocks the event loop. Cannot be used with async SQLAlchemy. | asyncpg for async PostgreSQL access. |
## Version Compatibility
| Package A | Compatible With | Notes |
|-----------|-----------------|-------|
| SQLAlchemy >=2.0.51 | asyncpg >=0.31.0 | Use `postgresql+asyncpg://` connection string. Some older asyncpg versions (0.29.x) had issues with `create_async_engine`. |
| essentia-tensorflow >=2.1b6.dev1438 | Python 3.14 | dev1438+ ships cp314 wheels only (macOS arm64/x86_64 + linux x86_64; no linux/arm64). Keep platform marker `sys_platform != 'linux' or platform_machine == 'x86_64'` in dependencies. |
| FastAPI >=0.139.0 | Pydantic >=2.10 | FastAPI requires Pydantic v2. Do not install Pydantic v1. |
| FastAPI >=0.139.0 | Starlette >=0.46.0 | Pinned by FastAPI. Do not override. |
| Alembic >=1.18.5 | SQLAlchemy >=2.0 | Use `alembic init -t async` for async template. Import all models in `env.py` for autogenerate to work. |
| litellm | ALL | **Pin exact minor line.** Supply chain attack on 1.82.7/1.82.8 (March 2026). Pinned `>=1.85.7,<1.86.0`; raise the cap deliberately after vetting. Verify SHA checksums. |
| SAQ >=0.26.4 (`saq[postgres]`) | Postgres (psycopg[binary]>=3.3.4) | Broker migrated from Redis to Postgres in Phase 36. Redis (client >=8.0.1) is used for caching only now. |
| chromaprint (system) | no verified consumer | Not consumed via `pyacoustid` (unused) or by any `phaze` source. **Not** an essentia-tensorflow runtime dependency either — `ldd` on the deployed `_essentia` extension shows no chromaprint link and `import essentia` succeeds without it (phaze-0jpe.6 correction; see `docs/design/0002-fingerprint-removal.md`). **Retained permanently by operator decision 2026-07-29** — phaze-0jpe.6 closed as "keep"; stays installed (`chromaprint-tools`) in Docker. |
## Confidence Assessment
| Area | Confidence | Reasoning |
|------|------------|-----------|
| Web framework (FastAPI) | HIGH | Verified current version, massive ecosystem, well-documented async patterns |
| Database (SQLAlchemy + asyncpg + Alembic) | HIGH | Standard production stack, verified versions, extensive async documentation |
| Audio metadata (mutagen) | HIGH | No real alternative for read+write. Stable, zero-dependency, widely used |
| Audio analysis (essentia-tensorflow) | HIGH | Comprehensive MIR library with pre-trained models for BPM, key, mood, style classification |
| Task queue (SAQ) | HIGH | Actively maintained, async-native, Redis-based. Drop-in replacement for arq with built-in web monitoring UI. |
| LLM integration (litellm) | MEDIUM | Best abstraction layer but recent supply chain incident is concerning. Pin versions aggressively, verify checksums |
| Web UI (HTMX + Jinja2) | HIGH | Well-proven pattern for Python admin tools. No build step, no JS framework complexity |
## Sources
- [mutagen on PyPI](https://pypi.org/project/mutagen/) -- version 1.47.0 verified
- [essentia on PyPI](https://pypi.org/project/essentia-tensorflow/) -- version 2.1b6.dev1438, used for audio analysis
- [FastAPI releases](https://github.com/fastapi/fastapi/releases) -- version 0.135.2 verified
- [SQLAlchemy on PyPI](https://pypi.org/project/SQLAlchemy/) -- version 2.0.48 verified
- [Alembic on PyPI](https://pypi.org/project/alembic/) -- version 1.18.4 verified
- [SAQ on PyPI](https://pypi.org/project/saq/) -- version 0.26.3, actively maintained
- [litellm security incident](https://docs.litellm.ai/blog/security-update-march-2026) -- supply chain attack March 2026
- [pydantic-settings on PyPI](https://pypi.org/project/pydantic-settings/) -- version 2.13.1 verified
- [HTMX + FastAPI patterns](https://johal.in/htmx-fastapi-patterns-hypermedia-driven-single-page-applications-2025/) -- 2025 production patterns
- [Python task queue benchmarks](https://stevenyue.com/blogs/exploring-python-task-queue-libraries-with-load-test) -- arq/dramatiq/huey performance comparison
<!-- GSD:stack-end -->

<!-- GSD:conventions-start source:CONVENTIONS.md -->
## Conventions

### No local identifiers in tracked files

phaze is developed against a real personal music archive on real hardware. Investigation output — spike docs, planning notes, debug write-ups, benchmark records — is where traces of that archive accumulate, because the honest way to report a measurement is to say what it was measured on.

**Do not commit them.** Scrub as you write, not afterwards.

**Never commit:** filenames, directory names or absolute paths from the real archive (the category that matters most — a release name in a log excerpt, a staging-mount path in a traceback, a directory name in a table of sampled files); content digests and file UUIDs taken from live data.

**Acceptable:** invented example filenames that illustrate a naming format (`Artist - Event - Title (2024).mp3`); synthetic test fixtures (`song.mp3`, `dup.mp3`); host and account names in local instruction material. Committed source, scripts and published docs should refer to hosts by role instead.

**Use these placeholders**, following the vocabulary already established in `docs/spikes/`:

| For | Use |
|-----|-----|
| Individual tracks | `<track-01>`, `<track-02>`, … |
| Concert sets / long recordings | `<set-01>`, `<set-02>`, … |
| Archive mount, host side | `<archive-mount>` |
| Archive mount, in-container | `<archive-mount-in-container>` |
| Local scratch directories | `<scratch>/…` |
| Fingerprint digests | `fp_<hash-1>`, `fp_<hash-2>`, … |
| File UUIDs | `<uuid-1>`, `<uuid-2>`, … |
| Hosts, where a role name will not do | `host-prod`, `host-store` |

**Replace identifiers, never quantities.** This is the rule that gets broken when scrubbing is rushed, and it destroys the value of the document it was meant to protect. Every measured value stays exact — row counts, durations, latencies, sample sizes, percentages. Good: "36 files totalling 42.34 h, stratified across the duration distribution". Bad: "a few dozen files" — scrubbed, but now worthless as evidence. If a scrub changes a number, it is a bug in the scrub; diff the numeric tokens before and after and confirm the only digits lost were part of a removed identifier.

**Scope:** any tracked file — spike and design docs, `.planning/**`, source comments, scripts, SQL. Also commit messages and PR bodies, which are just as permanent and just as public as the files.

**The history caveat:** scrubbing a file does not scrub git history. Once an identifier is committed, removing it from the working tree leaves it fully readable via `git show <old-sha>`, and removing it from history means a rewrite and a force-push — disruptive, and on a shared branch possibly not viable at all. **Prefer never committing the identifier over fixing it later:** use the placeholder in the first draft rather than the real name you intend to replace before pushing.
<!-- GSD:conventions-end -->

<!-- GSD:architecture-start source:ARCHITECTURE.md -->
## Architecture

Architecture not yet mapped. Follow existing patterns found in the codebase.
<!-- GSD:architecture-end -->

## Beadhive Workflow Enforcement

All work in this repo flows through beadhive. Do not make direct repo edits outside this workflow unless the user explicitly asks to bypass it.

1. **Every piece of work has a bead.** Larger work is an epic with specific stories/tasks/bugs as children. File epics through the planner (`bh plan file`), never by hand — hand-rolled epics fail the molecule convention check.
2. **Exploring a new idea?** Use the planner: invoke the `bh:planner` skill (`/bh:plan <idea>`) to drive ideate → research → decompose → file.
3. **When filing a new bead, ask clarifying questions** — scope, priority, acceptance — before writing the description.
4. **Before starting execution on a bead**, if there is any ambiguity about what must be delivered, keep asking clarifying questions until the work is clear.
5. **Once work starts, the dispatching session occupies the dispatcher seat itself** — load the `bh:dispatcher` skill and drive the molecule from that session; do NOT spawn a `bh:dispatcher` sub-agent (a sub-agent surrenders mid-flight visibility and leaves the session inferring state from git, which misreads both uncommitted work and evidence-only spike beads). From that seat, **dispatch a team of developer sub-agents**, each working in its own worktree (`wt/bead/issue/<id>`) branched off the bead's integration branch. Never share a worktree or a test database between concurrent agents.
6. **When all children of the bead are done:** open a PR, invoke a code review, and wait for green CI. If anything fails, investigate and fix — do not bypass. Once CI is green, merge to main (merge commit, never squash), then close the bead(s) with comments explaining the outcome.
7. **Periodically push the beads DB** to the Dolt remote: `bd dolt push`.

<!-- GSD:profile-start -->
## Developer Profile

> Profile not yet configured. Run `/gsd:profile-user` to generate your developer profile.
> This section is managed by `generate-claude-profile` -- do not edit manually.
<!-- GSD:profile-end -->

<!-- bv-agent-instructions-v3 -->
---

## Beads Workflow Integration

Work in this repo is tracked as **beads** in a local Dolt database under `.beads/`
(git-ignored) and synced to the Dolt remote `origin`
(`git+ssh://git@github.com/SimplicityGuy/phaze.git`). Beads are driven through
**beadhive** (`bh`); triage and graph analysis come from **beads_viewer**
(`bv`). See `## Beadhive Workflow Enforcement` above for the process rules — this
section is the command reference for them.

> **Editing note.** This block sits between two `bv` marker comments. `bv` detects it
> by marker and version only, never by content, so the prose here is free to diverge
> from `bv`'s stock text — but **do not touch, reword, or renumber the marker lines**,
> or `bv` starts prompting to add the section again at startup. Verify with
> `bv --agents-check`, which should report `blurb v3 — up to date` and exit 0.
> Be aware that `bv --agents-update` would overwrite everything below with `bv`'s
> generic boilerplate; don't run it unless you intend to lose this.

### The toolchain, and what is actually installed

| Tool | Status | Use it for |
|------|--------|------------|
| `bh` | installed | Everything lifecycle: reading, filing, claiming, validating, submitting, merging |
| `bv` | installed (v0.18.0) | Triage and graph analysis — *what to work on*, never mutation |
| `bd` | on `PATH` | Low-level beads CLI. Avoid calling directly (see below); the one sanctioned use is `bd dolt push` |

**The `bh bd` passthrough is disabled** (`passthrough.bd_enabled` defaults off), so
`bh bd <args>` errors out rather than forwarding. Calling `bd` directly is possible but
discouraged — a `PreToolUse` hook warns that it is not hive-aware and can hit the wrong
database. Read through `bh work`, file through `bh plan file` or the `bh` MCP tools, and
reach for `bd` only where this section says to.

### Reading beads

```bash
bh work ready                  # unblocked, dependency-ordered work
bh work list --status open     # filter by state; --json for machine output
bh work issue <id>             # one bead's fields, labels, model:/harness:
bh work brief <id>             # requirements + goals + the validation command
bh work show <id>              # the bead branch's local history before submit
```

All are read-only and accept `--json`.

### Filing beads

- **Epics / molecules** → the planner. Invoke the `bh:planner` skill (`/bh:plan <idea>`)
  or `bh plan file`. Never hand-roll an epic: it fails the molecule convention check.
- **A single bead** → the `bh` MCP tool `bd_create`, which auto-applies the
  provider/org/repo triplet and validates labels.
- Ask clarifying questions on scope, priority and acceptance *before* writing the
  description.

### Driving a bead

The lifecycle is `bh work`; raw `git` is only for the change *inside* the worktree.

```bash
bh work claim <id>       # provision the wt/bead/issue/<id> worktree + identity, → in_progress
bh work check <id>       # run the hive validation against the worktree
bh work submit <id>      # verify clean conventional history, re-validate from a
                         # pristine checkout, open the review gate
bh work approve <id>     # resolve the review gate
bh work merge <id>       # serialize a --no-ff merge onto the integration branch, close the bead
bh work resume <id>      # re-attach after changes-requested
bh work abandon <id>     # release the claim; --rm also removes the worktree
```

`submit` rejects noisy history — more than `max_commits` over base, or non-conventional
subjects. Use `bh work show <id>` to inspect and `bh work refine <id>` to squash local
checkpoints before resubmitting. `submit` publishes the branch only when the review gate
is `gh:run` / `gh:pr`; with the default in-process human gate it does not push, so open
the PR yourself.

### Triage with bv

`bv` is a graph-aware triage engine: dependency-aware, deterministic output with
precomputed metrics (PageRank, betweenness, critical path, cycles, HITS, eigenvector,
k-core). Use it instead of parsing `.beads/issues.jsonl` or guessing at graph traversal.

**Use only `--robot-*` flags. Bare `bv` launches an interactive TUI that blocks the session.**

```bash
bv --robot-triage                 # THE MEGA-COMMAND: start here
bv --robot-next                   # just the single top pick + claim command
bv --robot-triage --format toon   # token-optimized output for lower context cost
```

`--robot-triage` returns `quick_ref` (counts + top 3 picks), `recommendations` (ranked,
with unblock info), `quick_wins`, `blockers_to_clear`, `project_health` and `commands`.

Before claiming, confirm current state with `bh work issue <id>` or `bh work ready` —
`bv` reads an exported JSONL snapshot, so `bh` is the authority on live state.
`recommendations` can include graph-important work that is blocked or already assigned;
only `quick_ref.top_picks` and non-empty `claim_command` fields are actually claimable.

| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with unblocks lists |
| `--robot-priority` | Priority misalignment detection with confidence |
| `--robot-insights` | PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions, cycle breaks |
| `--robot-diff --diff-since <ref>` | Changes since ref: new/closed/modified |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |

```bash
bv --robot-plan --label backend        # scope to a label's subgraph
bv --robot-insights --as-of HEAD~30    # historical point-in-time
bv --recipe actionable --robot-plan    # pre-filter: ready to work (no blockers)
bv --recipe high-impact --robot-triage # pre-filter: top PageRank scores
```

### Key concepts

- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog. Use the numbers
  `0`–`4`, not words.
- **Types**: `bug`, `feature`, `task`, `epic`, `chore`, `decision`. Aliases:
  `enhancement`/`feat` → `feature`, `dec`/`adr` → `decision`. Anything else is rejected
  with `invalid issue type` unless registered under `types.custom`.
- **Dependencies**: beads can block other beads; `bh work ready` shows only unblocked
  work.
- **Worktrees**: one per bead, `wt/bead/issue/<id>`. Never share a worktree, a test
  database, or a Redis logical DB between concurrent agents.

### Syncing the Dolt remote

The JSONL export under `.beads/` is maintained for you — there is no flush step to run.
Periodically push the beads database itself:

```bash
bd dolt push
```

### Git policy

`bh` owns the lifecycle around the change; it does not absolve you of this repo's git
rules. Follow the repository's own instructions before staging, committing or pushing —
"commit only when asked" overrides any generic workflow advice, here or elsewhere.

<!-- end-bv-agent-instructions -->

<!-- bh:agf:start (managed by `bh hive init` — edit outside these markers; `-f` refreshes) -->
## AGF — Agentic Git Flow

This repo is onboarded as a **`bh` hive** and develops via **AGF**: work is tracked in beads
and driven through `bh`, **not** raw `git` / `bd` / `gh`.

- **Is this repo set up for AGF?** → run `bh hive ready` (add `-v` for the line-item breakdown).
- **Lifecycle, roles, conventions:** see `docs/AGF.md` and the bh plugin's role skills.
- Drive beads with `bh work`; load the role skill for your seat (coordinator / developer / merger).
- Batch/collapsed work lives in ONE shared `wt/batch/<group>` worktree and completes as a UNIT:
  `bh work submit --group` then `bh work merge --group` — per-bead `submit`/`check` don't apply.
<!-- bh:agf:end -->
