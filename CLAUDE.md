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
```

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
(`phaze test database: 'phaze_test' on localhost:5433`) — check it before trusting a green run.

**Never share Postgres OR Redis between concurrent agents.** Both are stateful, both are shared by
default, and both must be isolated per worktree. Saying "test database" here was the phaze-fwo7
defect: it taught agents to isolate Postgres and left every seat on the same logical Redis.

```bash
just test-db-for <name>    # creates phaze_<name>_test + phaze_<name>_migrations_test,
                           # allocates a dedicated Redis logical DB, and prints all three exports
```

Export all three lines it prints:

```bash
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<name>_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_<name>_migrations_test"
export PHAZE_REDIS_URL="redis://localhost:6380/<index>"
```

**Why Redis matters as much as Postgres.** Several redis-backed test modules run a global
`scan_iter`+`delete` sweep over `exec:*`, `exec_progress_req:*` and `tracklist_req:*` in fixture
setup *and* teardown. On a shared logical database one agent's fixture deletes another agent's live
keys mid-test, and assertions that count the keyspace see foreign keys. The result is a failure
indistinguishable from a real regression that passes on isolated re-run — the worst possible shape,
because it trains reviewers to dismiss red runs.

Redis DB indices are allocated from an atomic registry on the test container (DB 0 holds the
registry; seats get 1 upward), so re-running `test-db-for` for the same worktree is idempotent and
two worktrees can never collide. The container is started with 64 logical databases; allocation
past that fails loudly rather than wrapping onto a shared index. **Leaving `PHAZE_REDIS_URL` unset
is still valid for single-agent and CI runs** — it defaults to DB 0.

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

A music collection organizer that ingests music files (mp3, m4a, ogg) and concert video streams, fingerprints and analyzes them, uses AI to propose better filenames and destination paths, and provides an admin web UI to review and approve the renames/moves. Designed for a single user managing a large personal archive of music and live concert recordings (primarily full sets from events like Coachella).

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
| FastAPI | >=0.138.0 | Web framework / API | De facto standard for async Python APIs. Native async, auto-generated OpenAPI docs, Pydantic integration, SSE support for real-time UI updates. Massive ecosystem and community. |
| SQLAlchemy | >=2.0.51 | ORM / database toolkit | Industry standard Python ORM. Full async support via `create_async_engine` + asyncpg driver. Declarative models, relationship management, migration support via Alembic. |
| asyncpg | >=0.31.0 | PostgreSQL async driver | Fastest Python PostgreSQL driver. Purpose-built for asyncio. Used as SQLAlchemy's async backend. |
| Alembic | >=1.18.4 | Database migrations | Official SQLAlchemy migration tool. Async template support (`alembic init -t async`). Autogenerate from model changes. |
| PostgreSQL | 16+ (pinned to `postgres:18-alpine` in docker-compose/CI) | Primary database | Project constraint. Handles large-scale file metadata, complex queries, JSON columns for flexible metadata, full-text search for future features. |
| Redis | 8.x (client pinned `redis>=8.0.0,<9.0`) | Cache / pub-sub | No longer the SAQ broker (Phase 36 migrated the task queue to Postgres); used for caching analysis results and rate-limiting LLM API calls. |
| Docker Compose | 2.x | Deployment orchestration | Project constraint. Runs PostgreSQL, Redis, API server, worker processes as separate containers. |
### Audio / Music Libraries
| Library | Version | Purpose | Why Recommended |
|---------|---------|---------|-----------------|
| mutagen | >=1.47.0 | Audio metadata read/write | The standard for audio tag manipulation in Python. Supports ID3v1/v2, Vorbis, MP4, FLAC, OGG, AIFF. Zero dependencies. Read AND write capability needed for renaming workflows. |
| essentia-tensorflow | >=2.1b6.dev1438 | Audio feature extraction (BPM, key, mood, style) | Comprehensive MIR library with pre-trained TensorFlow models. Beat tracking, tempo estimation, key detection, mood/style classification. Used for all audio analysis in the main application. |
| pyacoustid | *(not a pyproject.toml dependency — not currently used)* | Audio fingerprinting | Originally recommended for Chromaprint/AcoustID bindings; the shipped fingerprinting pipeline (`services/audfprint`, `services/panako`) does not depend on it. |
| chromaprint (system) | latest | Fingerprint generation | C library (`libchromaprint`) required at runtime by essentia-tensorflow, not consumed via pyacoustid. Install via system package manager or include in Docker image. Provides `fpcalc` binary. |
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
| SAQ | >=0.26.4 (`saq[postgres]`) | Async task queue | Purpose-built for asyncio. Inspired by arq with active maintenance. Broker migrated from Redis to Postgres in Phase 36 (`PostgresQueue`, `saq_jobs` table). Perfect for file analysis jobs (BPM, fingerprinting, metadata extraction). Supports retries with backoff, job results, cron jobs, built-in web UI. Single-user app doesn't need Celery's complexity. |
### AI / LLM Integration
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| litellm | >=1.85.6,<1.86.0 (pin exact minor) | Unified LLM API client | Single interface to 100+ LLM providers (OpenAI, Anthropic, local models). Avoids vendor lock-in. Use for filename/path proposals. **IMPORTANT:** Pin exact minor line due to the March 2026 supply chain incident on versions 1.82.7-1.82.8. Verify checksums. |
| pydantic | >=2.10 | Data validation / LLM structured output | Already a FastAPI dependency. Use for validating LLM responses (proposed filenames, paths). Structured output parsing. |
### Configuration / Infrastructure
| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| pydantic-settings | >=2.14.2 | Configuration management | Type-safe config from env vars, .env files, Docker secrets. Native Pydantic integration. Supports `SecretStr` for API keys. |
| uvicorn | >=0.49.0 | ASGI server | Standard production server for FastAPI. Use with `--workers` for multi-process or behind gunicorn for production. |
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
| FastAPI >=0.138.0 | Pydantic >=2.10 | FastAPI requires Pydantic v2. Do not install Pydantic v1. |
| FastAPI >=0.138.0 | Starlette >=0.46.0 | Pinned by FastAPI. Do not override. |
| Alembic >=1.18.4 | SQLAlchemy >=2.0 | Use `alembic init -t async` for async template. Import all models in `env.py` for autogenerate to work. |
| litellm | ALL | **Pin exact minor line.** Supply chain attack on 1.82.7/1.82.8 (March 2026). Pinned `>=1.85.6,<1.86.0`; raise the cap deliberately after vetting. Verify SHA checksums. |
| SAQ >=0.26.4 (`saq[postgres]`) | Postgres (psycopg[binary]>=3.3.4) | Broker migrated from Redis to Postgres in Phase 36. Redis (client >=8.0.0) is used for caching only now. |
| chromaprint (system) | essentia-tensorflow | Not consumed via `pyacoustid` (unused) — `fpcalc`/`libchromaprint` is an essentia-tensorflow runtime dependency. Install `chromaprint-tools` in Docker. |
## Confidence Assessment
| Area | Confidence | Reasoning |
|------|------------|-----------|
| Web framework (FastAPI) | HIGH | Verified current version, massive ecosystem, well-documented async patterns |
| Database (SQLAlchemy + asyncpg + Alembic) | HIGH | Standard production stack, verified versions, extensive async documentation |
| Audio metadata (mutagen) | HIGH | No real alternative for read+write. Stable, zero-dependency, widely used |
| Audio analysis (essentia-tensorflow) | HIGH | Comprehensive MIR library with pre-trained models for BPM, key, mood, style classification |
| Audio fingerprinting (pyacoustid) | MEDIUM | Library works but hasn't released since 2023. Stable API, low maintenance risk, but monitor |
| Task queue (SAQ) | HIGH | Actively maintained, async-native, Redis-based. Drop-in replacement for arq with built-in web monitoring UI. |
| LLM integration (litellm) | MEDIUM | Best abstraction layer but recent supply chain incident is concerning. Pin versions aggressively, verify checksums |
| Web UI (HTMX + Jinja2) | HIGH | Well-proven pattern for Python admin tools. No build step, no JS framework complexity |
## Sources
- [mutagen on PyPI](https://pypi.org/project/mutagen/) -- version 1.47.0 verified
- [essentia on PyPI](https://pypi.org/project/essentia-tensorflow/) -- version 2.1b6.dev1438, used for audio analysis
- [pyacoustid on PyPI](https://pypi.org/project/pyacoustid/) -- version 1.3.0 verified
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

Conventions not yet established. Will populate as patterns emerge during development.
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

This project uses [beads_rust](https://github.com/Dicklesworthstone/beads_rust) (`br`) for issue tracking and [beads_viewer](https://github.com/Dicklesworthstone/beads_viewer) (`bv`) for graph-aware triage. Issues are stored in a local Dolt database under `.beads/` (git-ignored) and synced to the Dolt remote `origin` (`git+ssh://git@github.com/SimplicityGuy/phaze.git`) via `bd dolt push` / `bd dolt pull`. Current `br` workspaces normally export `.beads/issues.jsonl`; older `bd`/legacy workspaces may use `.beads/beads.jsonl`. `bv` auto-discovers the supported JSONL files, so agents should use `br`/`bv` commands instead of hard-coding a single filename.

### Using bv as an AI sidecar

bv is a graph-aware triage engine for Beads projects. Instead of parsing .beads/issues.jsonl / .beads/beads.jsonl directly or hallucinating graph traversal, use robot flags for deterministic, dependency-aware outputs with precomputed metrics (PageRank, betweenness, critical path, cycles, HITS, eigenvector, k-core).

**Scope boundary:** bv handles *what to work on* (triage, priority, planning). `br` handles creating, modifying, and closing beads.

**CRITICAL: Use ONLY --robot-* flags. Bare bv launches an interactive TUI that blocks your session.**

#### The Workflow: Start With Triage

**`bv --robot-triage` is your single entry point.** It returns everything you need in one call:
- `quick_ref`: at-a-glance counts + top 3 picks
- `recommendations`: ranked actionable items with scores, reasons, unblock info
- `quick_wins`: low-effort high-impact items
- `blockers_to_clear`: items that unblock the most downstream work
- `project_health`: status/type/priority distributions, graph metrics
- `commands`: copy-paste shell commands for next steps

```bash
bv --robot-triage        # THE MEGA-COMMAND: start here
bv --robot-next          # Minimal: just the single top pick + claim command

# Token-optimized output (TOON) for lower LLM context usage:
bv --robot-triage --format toon
```

Before claiming, verify current state with `br show <id> --json` or `br ready --json`. `recommendations` can include graph-important blocked or assigned work; only `quick_ref.top_picks` and non-empty `claim_command` fields represent claimable work.

#### Other bv Commands

| Command | Returns |
|---------|---------|
| `--robot-plan` | Parallel execution tracks with unblocks lists |
| `--robot-priority` | Priority misalignment detection with confidence |
| `--robot-insights` | Full metrics: PageRank, betweenness, HITS, eigenvector, critical path, cycles, k-core |
| `--robot-alerts` | Stale issues, blocking cascades, priority mismatches |
| `--robot-suggest` | Hygiene: duplicates, missing deps, label suggestions, cycle breaks |
| `--robot-diff --diff-since <ref>` | Changes since ref: new/closed/modified issues |
| `--robot-graph [--graph-format=json\|dot\|mermaid]` | Dependency graph export |

#### Scoping & Filtering

```bash
bv --robot-plan --label backend              # Scope to label's subgraph
bv --robot-insights --as-of HEAD~30          # Historical point-in-time
bv --recipe actionable --robot-plan          # Pre-filter: ready to work (no blockers)
bv --recipe high-impact --robot-triage       # Pre-filter: top PageRank scores
```

### br Commands for Issue Management

```bash
br ready --json                       # Show issues ready to work (no blockers)
br list --status=open --json          # All open issues
br show <id> --json                   # Full issue details with dependencies
br create --title="..." --type=task --priority=2 --json
br update <id> --status=in_progress --json
br close <id> --reason="Completed" --json
br close <id1> <id2> --reason="Completed" --json
br sync --flush-only                  # Export DB to JSONL after Beads mutations
```

### Workflow Pattern

1. **Triage**: Run `bv --robot-triage` to find the highest-impact actionable work
2. **Claim**: Use `br update <id> --status=in_progress --json`
3. **Work**: Implement the task
4. **Complete**: Use `br close <id> --reason="Completed" --json`
5. **Sync**: Run `br sync --flush-only` after Beads mutations so the JSONL export is current

### Key Concepts

- **Dependencies**: Issues can block other issues. `br ready --json` shows only unblocked work.
- **Priority**: P0=critical, P1=high, P2=medium, P3=low, P4=backlog (use numbers 0-4, not words)
- **Types**: task, bug, feature, epic, chore, docs, question
- **Blocking**: `br dep add <issue> <depends-on>` to add dependencies

### Git Policy

`br` never commits or pushes. Follow this repository's own git instructions before staging, committing, or pushing. If the repository says "commit only when asked," that rule overrides any generic workflow advice.

<!-- end-bv-agent-instructions -->
