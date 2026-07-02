# Phase 63: Parallel CI & Code-Change Gating - Research

**Researched:** 2026-07-02
**Domain:** CI/build engineering — pytest suite partitioning, GitHub Actions matrix fan-out, coverage.py combine, change-gating
**Confidence:** HIGH (coverage/xdist mechanics, GH Actions patterns, existing-code facts) / MEDIUM (two locked-decision-vs-reality tensions surfaced below need a planning call)

## Summary

Phase 63 restructures the existing single `tests.yml` job into a parallel, per-bucket matrix
with one trustworthy combined coverage upload, plus a broadened doc-only skip. The coverage-combine
plumbing (CI-03) and the change-gate (CI-04) are well-trodden, low-risk patterns with authoritative
recipes — `coverage.py` `parallel`/`relative_files` + `coverage combine` + artifact upload/download,
and the already-built `detect-changes`/`aggregate-results` skip-with-success contract. Those parts
are mechanical.

The **risk concentrates entirely in the directory reorg (CI-01)**, and two facts from the codebase
change the shape of the plan:

1. **Basename collisions already exist** (`test_fingerprint.py`, `test_pipeline.py`, `test_proposal.py`,
   … appear in 2+ directories) and work today *only* because every test dir is an `__init__.py`
   package under `import-mode=prepend`, giving each file a distinct dotted module name. Flattening
   files into `tests/<bucket>/` domain dirs will create **same-directory** basename collisions that
   break collection. This is the single biggest reorg hazard.
2. **117 of 212 test files (55%) consume DB fixtures** (`client`, `session`, `async_engine`, the
   `seed_*` factories) and are therefore auto-marked `integration` by `tests/conftest.py`. Only 5 of
   those 117 live under `tests/integration/` or `tests/test_migrations/` today. This directly
   conflicts with D-07's "unit buckets run service-free" and with D-01's "`-n auto` inside each
   bucket" (the shared-`phaze_test` `create_all`/`drop_all` fixture races under xdist). Both need a
   planning decision — see **Open Questions Q-A and Q-B**.

**Primary recommendation:** Put the bucket **matrix inside `tests.yml`** (not `ci.yml`) so `ci.yml`'s
job graph and the branch-protection required check (`aggregate-results`) stay unchanged. Every bucket
job provisions Postgres+Redis (services are cheap on free runners; 55% DB penetration makes
"service-free unit buckets" impractical). Get matrix fan-out parallelism first; add intra-bucket
`-n auto` **only on verified DB-free buckets** unless per-worker DB isolation is built. Coverage:
add `relative_files = true` (keep `concurrency = ["greenlet","thread"]` — do **not** add
`multiprocessing`), each bucket writes `COVERAGE_FILE=.coverage.<bucket>`, a final combine job runs
`coverage combine` + `coverage xml` + `coverage report --fail-under=85` → single Codecov upload.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Hybrid — GitHub job matrix over buckets, `pytest-xdist` (`-n auto`) inside each bucket. Matrix gives CI-01 its independently-runnable buckets; xdist adds intra-bucket parallelism.
- **D-02:** Coverage combined in two stages — xdist auto-combines within a job; matrix jobs each emit `.coverage.<bucket>`; a final combine job runs `coverage combine` + `coverage xml` → single Codecov upload.
- **D-03:** Directory reorg + path-glob selection. Physically relocate `test_*.py` into `tests/<bucket>/`; matrix selects a bucket by path. Chosen over conftest auto-marking for structural exclusivity.
- **D-04:** Buckets: `discovery`, `metadata`, `fingerprint`, `analyze`, `identify`, `review`, `agents`, `integration`, and `shared` (generic catch-all: schema, config, helpers, routing).
- **D-05:** Shared helpers and the root `conftest.py` stay at `tests/` root (`conftest.py`, `_queue_fakes.py`, `_route_introspection.py`, `kube_fakes.py`). Only `test_*.py` files relocate. Hierarchical conftest + absolute imports keep working.
- **D-06:** Add a partition-guard test asserting every collected `test_*.py` resides under a known bucket directory. Path check, not marker check.
- **D-07:** Dedicated `integration` bucket/job owns `-m integration` (auto-marked by path today) and provisions postgres+redis (reuse `tests.yml`). Unit buckets run service-free. Integration `.coverage` still folds into the combined report.
- **D-08:** Broaden the existing skip rule — extend the skippable set to `.planning/**`, `LICENSE`, other non-source docs (not just `*.md`); document + lock the required-check contract (skip-with-success, never skip-absent).
- **D-09:** Keep the changed-files gate job (not bare `paths-ignore`). Add regression coverage for the gate logic.
- **D-10:** All new CI steps delegate to `just` recipes. Expect `just test-bucket <name>` and `just coverage-combine`. Keep `justfile` and `scripts/update-project.sh` current.

### Claude's Discretion
- Exact matrix YAML shape, artifact names, `coverage combine` invocation, whether combine job re-runs Codecov action or a dedicated upload step.
- `-n auto` vs fixed worker count per bucket; verify xdist's multiprocessing combine works with `concurrency = ["greenlet","thread"]`.
- Exact `just` recipe names/signatures.
- Single `codecov` flag vs per-bucket flags on the single combined upload (requirement is one upload; flags are cosmetic).

### Deferred Ideas (OUT OF SCOPE)
- Coverage gate **raise** + per-module uplift → Phase 64 (COV-01/02). Phase 63 only makes the combined number *correct*.
- REQUIREMENTS traceability CI gate, `/saq` re-link, dead-code sweep → Phase 66.
- pytest-cov → other coverage tooling swap → explicitly out of scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| CI-01 | Suite partitioned into independently-runnable per-workflow-step buckets, each selectable in isolation | Directory reorg (D-03/04) + `just test-bucket <name>` → `pytest tests/<bucket>`. **Reorg hazards:** basename collisions + `from tests.test_migrations.conftest` imports (Pitfalls 1–3). Bucket assignment is semantic/manual — filename keywords ≠ bucket names ("discovery" = 0 files but "scan" = 9). |
| CI-02 | Fan buckets out across parallel jobs, measurably cutting wall-clock | GH Actions `strategy.matrix.bucket` inside `tests.yml`. Matrix fan-out is the primary win; intra-bucket `-n auto` gated on DB-safety. Measure via `gh run view --json jobs` durations before/after (Code Examples §5). |
| CI-03 | Per-shard `.coverage` combined into ONE report + ONE Codecov upload, no loss, no double-count | `coverage.py` `parallel`+`relative_files`, `COVERAGE_FILE=.coverage.<bucket>`, artifact up/download, `coverage combine` + `coverage xml` + `coverage report --fail-under=85` in a final combine job (Code Examples §2–3). Keep `concurrency=["greenlet","thread"]`; **do not** add `multiprocessing`. |
| CI-04 | Docs/`.planning/`/markdown-only PR skips heavy jobs while required checks report SUCCESS (skip-with-success) | Broaden `detect-changes` file classifier in `ci.yml` (currently `grep -v '\.md$'`); keep `aggregate-results` as the single stable required check; add regression tests for the classifier (Code Examples §4). |
</phase_requirements>

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Bucket selection / test layout | Test suite (`tests/`) | `justfile` | Directory == bucket; `just test-bucket` maps name→path |
| Parallel fan-out | GitHub Actions (`tests.yml` matrix) | pytest-xdist | Matrix = inter-bucket parallelism; xdist = intra-bucket |
| Coverage combine + gate | `coverage.py` (combine job in `tests.yml`) | Codecov (report sink) | Combine is local/authoritative; Codecov is display only |
| Change-gating | GitHub Actions (`ci.yml` `detect-changes`/`aggregate-results`) | — | Orchestrator owns skip-with-success; required check = aggregate |
| Required-check contract | Branch protection (repo settings, maintainer) | `ci.yml` job names | Must point at a *stable* aggregate job, not matrix legs |

## Standard Stack

### Core (already present unless noted)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pytest | >=9.1.1 (installed) | Test runner | Project standard `[CITED: pyproject.toml]` |
| pytest-cov | >=7.1.0 (installed) | Coverage integration + xdist worker merge | Handles xdist coverage without extra config `[CITED: pyproject.toml]` |
| coverage.py | (via pytest-cov) | `combine`/`xml`/`report` | `coverage combine` is the canonical multi-file merge `[CITED: coverage.readthedocs.io/en/latest/commands/cmd_combine.html]` |
| **pytest-xdist** | **>=3.8.0 (ADD to dev group)** | Intra-bucket parallelism (`-n auto`) | pytest-dev official; not currently a dependency `[VERIFIED: absent from uv.lock]`; latest 3.8.0 `[VERIFIED: pip index versions]` |
| just | (installed via `extractions/setup-just`) | CI command delegation | Project convention (D-10) `[CITED: justfile]` |
| Codecov action | codecov-action v7.0.0 (pinned SHA) | Single combined upload | Already used in `tests.yml` `[CITED: tests.yml:83]` |

### Supporting (GitHub Actions, verify current SHA before pinning)
| Action | Purpose | When to Use |
|--------|---------|-------------|
| `actions/upload-artifact` (v4+) | Upload each bucket's `.coverage.<bucket>` | Per matrix leg. Needs `include-hidden-files: true` (dotfile) + `if-no-files-found` |
| `actions/download-artifact` (v4+) | Pull all shard data into combine job | v4+ required for `pattern:` + `merge-multiple: true` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Directory-based buckets | `pytest -m <marker>` selection | Marker split allows zero/two-bucket leakage; D-03 chose dirs for structural exclusivity. But see Q-A: dirs + 55% DB tests reintroduce a service-provisioning problem markers would have localized. |
| Matrix inside `tests.yml` | Matrix at `ci.yml` level | ci.yml-level matrix changes `needs.test`/required-check names → touches branch protection. Inside `tests.yml` keeps ci.yml graph and required check **unchanged** (recommended). |
| xdist `-n auto` in every bucket | Serial buckets, matrix-only parallelism | xdist races the shared `phaze_test` DB (Pitfall 4). Serial is safe; matrix already delivers the CI-02 win. |
| coverage.py combine (local, authoritative) | Codecov server-side merge | Codecov merge is display-only and can't enforce `fail_under` before Phase 64; local combine is the source of truth. |

**Installation:**
```bash
uv add --dev "pytest-xdist>=3.8.0"   # verify publish date > 7 days (uv exclude-newer cooldown) before locking
```

**Version verification:**
```bash
python3 -m pip index versions pytest-xdist   # → 3.8.0 latest (verified 2026-07-02)
```
`[tool.uv] exclude-newer = "7 days"` — if 3.8.0 is < 7 days old at lock time, resolution fails; pin the floor to an older release (3.7.0 / 3.6.1) that clears the cooldown. `[CITED: pyproject.toml:197]`

## Package Legitimacy Audit

> slopcheck was unavailable in this session (no network install attempted per sandbox). pytest-xdist
> is the only new package; it is a pytest-dev official package with a long history — low risk — but
> is tagged `[ASSUMED]` and the planner should gate the `uv add` behind normal review.

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| pytest-xdist | PyPI | mature (v1.0→3.8.0) | very high | github.com/pytest-dev/pytest-xdist | not run | Approved `[ASSUMED]` — verify publish date vs cooldown |

**Packages removed due to slopcheck [SLOP]:** none
**Packages flagged [SUS]:** none

## Architecture Patterns

### System Architecture Diagram

```
                        ┌─────────────── ci.yml (orchestrator, UNCHANGED graph) ───────────────┐
   PR / push ──▶ detect-changes ──code-changed?──▶  quality ──▶ test (reusable: tests.yml)
                        │  (broaden classifier:            │         security                  │
                        │   .md + .planning/** + LICENSE)  │         docker                    │
                        └──────────────┬───────────────────┴───────────┬───────────────────────┘
                                       ▼                                ▼
                              aggregate-results  ◀── needs: test, quality, security, docker
                              (if: always(); skip-with-success)   ── THE required check ──▶ branch protection
                                       │
                                       ▼  (code-changed==true)
                              docker-publish

   ┌──────────────────────── tests.yml (reusable — matrix ADDED here) ─────────────────────────┐
   │  strategy.matrix.bucket: [discovery, metadata, fingerprint, analyze,                       │
   │                            identify, review, agents, integration, shared]                  │
   │                                                                                            │
   │  bucket job (×N in parallel)                    combine job (needs: all bucket jobs)       │
   │  ─ services: postgres+redis                     ─ download-artifact pattern coverage-*     │
   │  ─ COVERAGE_FILE=.coverage.<bucket>               merge-multiple: true                     │
   │  ─ just test-bucket <bucket>                     ─ just coverage-combine                    │
   │      = pytest tests/<bucket> --cov=phaze          = coverage combine                       │
   │        --cov-report= [-n auto if DB-free]           coverage xml                           │
   │  ─ upload-artifact .coverage.* → coverage-<b>      coverage report --fail-under=85         │
   │                                                  ─ codecov-action (ONE upload, coverage.xml)│
   └────────────────────────────────────────────────────────────────────────────────────────────┘
```

### Recommended Project Structure (post-reorg)
```
tests/
├── __init__.py                 # STAYS (root package — enables `from tests....`)
├── conftest.py                 # STAYS at root (D-05, hierarchical)
├── _queue_fakes.py             # STAYS (imported as tests._queue_fakes ×24)
├── _route_introspection.py     # STAYS (tests._route_introspection ×6)
├── kube_fakes.py               # STAYS (tests.kube_fakes ×1)
├── discovery/    __init__.py   # scan/watcher/discovery test_*.py
├── metadata/     __init__.py
├── fingerprint/  __init__.py
├── analyze/      __init__.py
├── identify/     __init__.py   # identify + tracklist
├── review/       __init__.py   # review + apply + proposals
├── agents/       __init__.py   # agent_* + deployment + distributed (~48 candidate files)
├── integration/  __init__.py   # EXISTING real-PG queue tests (+ test_migrations? see Q-A)
│   └── conftest.py             # EXISTING integration harness — stays scoped here
└── shared/       __init__.py   # schema, config, models, routing, helpers-tests, catch-all
```
Every bucket dir needs `__init__.py` (import-mode=prepend requires packages; see Pitfall 1).
To avoid same-dir basename collisions, either preserve one level of sub-nesting inside a bucket
(e.g. `tests/analyze/services/…` + `tests/analyze/routers/…`, each a package) or rename colliding
files on move. The partition-guard checks the **top** path segment (`item.path` relative to `tests/`).

### Pattern 1: Matrix inside the reusable workflow (keeps required check stable)
**What:** Add `strategy.matrix` to the `test` job *inside* `tests.yml`; add a `combine` job in the
same file that `needs` the matrix job. `ci.yml`'s `test:` node stays a single reusable-workflow call.
**When:** Always here — it is what keeps `aggregate-results` (the required check) and branch
protection untouched.
**Why:** A reusable-workflow call's result is `success` only if all matrix legs + combine succeed,
and `skipped` when `detect-changes` gates it off — exactly what `aggregate-results` already consumes
via `needs.test.result`. `[CITED: ci.yml:88-119]`

### Pattern 2: Single-source bucket list (avoid matrix/guard/justfile drift)
**What:** Declare buckets once and consume everywhere.
**Options:**
- (a) Hardcode `strategy.matrix.bucket: [...]` in `tests.yml`; the partition-guard derives the valid
  set from the immediate subdirs of `tests/` and asserts matrix ⊇ dirs. Small, explicit.
- (b) `tests/buckets.json` consumed by the matrix (`fromJSON`), `just test-bucket` validation, and
  the partition-guard. Zero drift; slightly more wiring. **Recommended** for a true single source.
- `just test-bucket <name>` maps name→path by convention: `pytest tests/<name>`.

### Anti-Patterns to Avoid
- **Combining coverage server-side in Codecov instead of locally.** Codecov cannot enforce
  `fail_under` before Phase 64 raises it; the authoritative number must come from `coverage report`.
- **Making per-bucket matrix leg checks required in branch protection.** Their names vary and they
  skip on doc-only PRs → the "required check never runs → unmergeable" trap. Only the stable
  `aggregate-results` job is required (D-08/D-09).
- **`paths-ignore` at the workflow trigger for CI-04.** That produces skip-*absent* (required check
  never reports) — the exact trap D-09 forbids. Keep the `detect-changes` gate job.
- **`-n auto` on DB-backed buckets without per-worker DB isolation.** Races `create_all`/`drop_all`
  on the shared `phaze_test` (Pitfall 4).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Merging N shard coverage files | Custom `.coverage` SQLite merge script | `coverage combine` | Matches by source path, handles parallel suffixes, battle-tested `[CITED: cmd_combine.html]` |
| Cross-checkout path mapping | `sed` rewriting paths in coverage data | `[run] relative_files = true` (+ `[paths]` if needed) | Documented mechanism for combining across machines/paths |
| Unique per-shard data filenames | Manual timestamp/random names | `COVERAGE_FILE=.coverage.<bucket>` or `[run] parallel = true` | Coverage's built-in distinct-file naming |
| Detecting doc-only changes | New workflow from scratch | Extend existing `detect-changes` `grep` classifier | CI-04 is ~80% built (handles PR/new-branch/force-push SHA edge cases) `[CITED: ci.yml:33-80]` |
| Intra-bucket parallelism | Threading in test code | `pytest-xdist -n` | Standard, integrates with pytest-cov worker merge |

**Key insight:** Every hard part of CI-03/CI-04 already has a canonical tool or an 80%-built asset in
this repo. The genuinely custom work is the *file→bucket assignment* (manual, semantic) and the
*safe reorg mechanics* (imports + collisions).

## Runtime State Inventory

> This is a refactor/reorg phase (moving `test_*.py` files, changing CI). No product runtime state,
> but the reorg has code-level state to track.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — no DB/datastore keys reference test file paths. Verified: coverage config keys on `source=["phaze"]`, not test paths. | none |
| Live service config | **Branch protection required-check names** (GitHub repo settings, NOT in git). If the required check is currently a `tests.yml`/`Tests` job name, it may change when the matrix is added. | Maintainer must confirm/repoint required check to the stable `aggregate-results` job. Document in PR. |
| OS-registered state | None. | none |
| Secrets/env vars | `CODECOV_TOKEN`, `GITHUB_TOKEN` — unchanged; combine job needs `CODECOV_TOKEN` where the upload moves to. `COVERAGE_FILE` is a new *build-time* env, not a secret. | Move Codecov step (with token) from per-bucket into the combine job. |
| Build artifacts / installed packages | (1) `tests/**/__pycache__` — stale after `git mv`; harmless, regenerated. (2) `pytest-xdist` not yet installed. (3) `scripts/update-project.sh` must learn any new `just` recipes (feedback rule). | `uv add --dev pytest-xdist`; update `scripts/update-project.sh`; ignore/clean pycache. |

**Cross-file import state (the load-bearing one):** `from tests.test_migrations.conftest import …`
(13 sites), `from tests._queue_fakes` (24), `from tests._route_introspection` (6), `from tests.conftest`
(2), `from tests.kube_fakes` (1). Root-level helpers stay put (D-05) so 33 of these are unaffected.
The **13 `tests.test_migrations.conftest` imports break** if `test_migrations/` moves under a bucket
(e.g. `tests/integration/test_migrations/`) — those imports must be rewritten to the new dotted path.

## Common Pitfalls

### Pitfall 1: Same-directory basename collisions after flattening (COLLECTION-BREAKING)
**What goes wrong:** `test_fingerprint.py`, `test_pipeline.py`, `test_proposal.py`, `test_execution.py`,
and ~15 others already exist in 2+ directories. Under `import-mode=prepend` with `__init__.py`
packages they coexist as distinct dotted modules (`tests.test_services.test_fingerprint` vs
`tests.test_routers.test_fingerprint`). If two same-named files land in the **same** `tests/<bucket>/`
dir, pytest cannot import both — `import file mismatch` / duplicate-module error.
**Why it happens:** Domain buckets aggregate files currently split across code-layer dirs
(`test_services`, `test_routers`, `test_tasks`).
**How to avoid:** Either (a) preserve one level of sub-nesting inside each bucket (each a package with
`__init__.py`) so dotted names stay unique, or (b) rename colliding files on move
(`test_fingerprint_service.py` / `test_fingerprint_router.py`). Do NOT switch to `import-mode=importlib`
mid-reorg — it drops `__init__.py` reliance and would break the `from tests.…` absolute imports unless
`consider_namespace_packages=true` is also set (larger blast radius).
**Warning signs:** `pytest tests/<bucket>` errors with "import file mismatch" or "already imported".
**Detection command (run BEFORE wiring the matrix):**
`find tests -name 'test_*.py' | xargs -n1 basename | sort | uniq -d` → must be empty *within any single
bucket dir* after the move.

### Pitfall 2: `tests.test_migrations.conftest` imports break on move
**What goes wrong:** 13 files do `from tests.test_migrations.conftest import …`. Moving
`test_migrations/` changes its dotted path → `ModuleNotFoundError`.
**How to avoid:** Rewrite the 13 imports to the new path (mechanical `sed`), or keep `test_migrations/`
at a stable location. Run the full suite green **after** the reorg and **before** touching CI (per the
D-specific "keep it mechanical" note) so a broken import can't hide behind a CI change.
**Warning signs:** `ModuleNotFoundError: No module named 'tests.test_migrations'`.

### Pitfall 3: The path auto-marker depends on literal dir names
**What goes wrong:** `tests/conftest.py:132-135` auto-marks `integration` when
`"test_migrations" in path_parts` OR `"integration" in path_parts` OR a DB fixture is used. If the
integration bucket is renamed away from `integration`, or `test_migrations` is renamed, tests silently
lose the marker and run against a missing DB in the wrong job.
**How to avoid:** Keep the integration bucket dir literally named `integration` (D-04 does). If
`test_migrations/` moves under `integration/`, both `"integration"` and `"test_migrations"` remain in
`path_parts` → marker still applied (verified by inspection). The DB-fixture trigger is fixture-name
based and **path-independent** — unaffected by any move.
**Warning signs:** `pytest -m 'not integration'` suddenly tries to connect to Postgres and errors.

### Pitfall 4: xdist `-n auto` races the shared `phaze_test` database (FLAKY)
**What goes wrong:** `async_engine` (conftest.py:147-157) runs `Base.metadata.create_all` then
`drop_all` against a single `TEST_DATABASE_URL`. Under `-n auto`, xdist workers execute concurrently
against the *same* DB → concurrent DDL on the same tables → intermittent failures.
**Why it happens:** D-01 mandates `-n auto` inside each bucket, but the fixture assumes a single
writer. 117/212 files touch this fixture.
**How to avoid (pick one):**
- (Low effort, recommended first) Matrix fan-out is the primary parallelism; run DB-backed buckets
  **serially** (no `-n`), apply `-n auto` only to verified DB-free buckets (`shared` unit tests,
  pure-logic buckets). Pass a per-bucket xdist flag through `just test-bucket`.
- (Full D-01 fidelity) Give each xdist worker its own database: derive the DB name from
  `PYTEST_XDIST_WORKER` (e.g. `phaze_test_gw0`), create the N databases in the service-init step, and
  make `async_engine`/`TEST_DATABASE_URL` worker-aware. Higher effort; treat as an optional later step.
**Warning signs:** Non-deterministic `DuplicateTable`/`UndefinedTable`/deadlock errors that change
run-to-run (mirrors the known colima full-suite flake, but here it would be a real reorg regression).

### Pitfall 5: Coverage double-counting / loss across shards
**What goes wrong:** If a file is collected in two buckets it is counted twice (inflates coverage); if
in zero buckets it is dropped (deflates). Either makes the CI-03 number untrustworthy — and Phase 64's
gate would sit on a wrong baseline.
**How to avoid:** The directory partition makes each file live in exactly one bucket (D-03); the
partition-guard (D-06) fails CI if any collected test escapes a bucket dir. `coverage combine` matches
by source path and unions line data (no double-count even if the same *source* line is hit in two
buckets — coverage is a set union, not a sum). Verify the combined `coverage report` total is within
tolerance of a pre-reorg full-run baseline as an acceptance check.
**Warning signs:** Combined total jumps or drops vs the ~90.38% baseline for no code reason.

### Pitfall 6: `relative_files` + duplicate nested dir names data loss (LOW risk here)
**What goes wrong:** coverage.py issue #2072 — `relative_files=true` can collapse nested directories
that share a name, losing data.
**Why it's low risk:** This affects the **source tree** being measured. `source = ["phaze"]` (src/phaze)
has no duplicate-named nested package dirs. Still, verify the combined report enumerates the expected
`phaze/*` modules after enabling `relative_files`.

## Code Examples

### 1. `pyproject.toml` — coverage config (add `relative_files`, keep concurrency)
```toml
# Source: coverage.readthedocs.io/en/latest/config.html
[tool.coverage.run]
concurrency = ["greenlet", "thread"]   # KEEP. Do NOT add "multiprocessing":
                                       # xdist workers are execnet processes managed by
                                       # pytest-cov, not multiprocessing children of the code
                                       # under test. greenlet+thread cover SQLAlchemy async.
omit = ["tests/*"]
source = ["phaze"]
relative_files = true                  # ADD: combine data from N jobs by relative path
# parallel = true                      # OPTIONAL: unique-suffix data files. Not required if
                                       # each job sets COVERAGE_FILE=.coverage.<bucket>.
```

### 2. `justfile` — new bucket + combine recipes (delegation, D-10)
```makefile
# Run one bucket. XDIST defaults to "" (serial, DB-safe); DB-free buckets pass XDIST="-n auto".
[doc('Run a single test bucket with coverage data output (CI shard)')]
[group('test')]
test-bucket NAME XDIST="":
    COVERAGE_FILE=.coverage.{{NAME}} uv run pytest tests/{{NAME}} {{XDIST}} \
        --cov=phaze --cov-report= -q

[doc('Combine per-bucket .coverage.* shards into coverage.xml and enforce the gate')]
[group('test')]
coverage-combine:
    uv run coverage combine
    uv run coverage xml
    uv run coverage report --fail-under=85
```

### 3. `tests.yml` — matrix + combine (shape; freeze SHAs before commit)
```yaml
# Source pattern: hynek.me/articles/ditch-codecov-python + scientific-python coverage guide
jobs:
  test:
    strategy:
      fail-fast: false
      matrix:
        bucket: [discovery, metadata, fingerprint, analyze, identify, review, agents, integration, shared]
    runs-on: ubuntu-latest
    services: { postgres: {...}, redis: {...} }   # lift existing tests.yml block verbatim
    steps:
      - uses: actions/checkout@<sha>
      - uses: astral-sh/setup-uv@<sha>
      - uses: actions/setup-python@<sha>
      - uses: extractions/setup-just@<sha>
      - run: |
          psql -h localhost -U phaze -d postgres -c 'CREATE DATABASE phaze_migrations_test OWNER phaze;'
        env: { PGPASSWORD: phaze }
      - run: just install
      - run: just test-bucket ${{ matrix.bucket }}        # add XDIST for DB-free buckets
        env: { DATABASE_URL: ..., TEST_DATABASE_URL: ..., PHAZE_REDIS_URL: ... }
      - uses: actions/upload-artifact@<sha>   # v4+
        with:
          name: coverage-${{ matrix.bucket }}
          path: .coverage.${{ matrix.bucket }}
          include-hidden-files: true
          if-no-files-found: error            # a bucket producing no data = a bug, fail loud

  combine:
    needs: [test]
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@<sha>
      - uses: astral-sh/setup-uv@<sha>
      - uses: extractions/setup-just@<sha>
      - run: just install
      - uses: actions/download-artifact@<sha>  # v4+
        with: { pattern: coverage-*, merge-multiple: true }
      - run: just coverage-combine             # coverage combine + xml + report --fail-under=85
      - uses: codecov/codecov-action@fb8b3582c8e4def4969c97caa2f19720cb33a72f  # v7.0.0 (reuse pin)
        with: { flags: unittests, disable_search: true, files: ./coverage.xml }
        env: { CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }} }
```

### 4. `ci.yml` — broaden the doc-only classifier (CI-04)
```bash
# Replace: NON_MD_FILES=$(echo "${CHANGED_FILES}" | grep -v '\.md$' || true)
# With an explicit non-source (skippable) filter — anything NOT matching stays as "code":
CODE_FILES=$(echo "${CHANGED_FILES}" | grep -vE \
  '(\.md$|^\.planning/|^LICENSE$|^docs/|\.txt$)' || true)
if [[ -z "${CODE_FILES}" ]]; then
  echo "code-changed=false" >> "${GITHUB_OUTPUT}"
else
  echo "code-changed=true"  >> "${GITHUB_OUTPUT}"
fi
```
Add a regression test (D-09) that feeds representative changed-file lists (all-`.planning`, mixed,
`LICENSE`-only, a `.py` change) through the classifier logic and asserts the `code-changed` output.
Extract the classifier to a small tested shell/Python script so it is unit-testable.

### 5. Measuring "materially faster" (CI-02 evidence)
```bash
# Before (baseline) and after (matrix) on a representative code-change run:
gh run view <run-id> --json jobs \
  --jq '.jobs[] | select(.name|test("Tests")) | {name, started:.startedAt, ended:.completedAt}'
# Wall-clock = max(bucket-job durations) + combine-job duration, vs the old single Tests job.
# Cite the delta in the PR description as CI-02 evidence.
```

## State of the Art

| Old Approach | Current Approach | When | Impact |
|--------------|------------------|------|--------|
| Single serial `Tests` job (`just test-ci`, one Codecov upload) | Bucket matrix + local `coverage combine` + one upload | This phase | Faster wall-clock; combine must be correct before Phase 64 raises the gate |
| `paths-ignore` for doc skips | Changed-files **gate job** (skip-with-success) | already in repo (`detect-changes`) | Keeps required checks satisfiable; phase only broadens the classifier |
| Codecov v3/v4 auto-search | codecov-action v7 with `disable_search: true`, explicit `files:` | already in repo | One deterministic upload of the combined xml |
| upload/download-artifact v3 | v4+ (`pattern:` + `merge-multiple:`) | GH deprecated v3 (2024) | Combine job pulls all shards in one step |

**Deprecated/outdated:** artifact actions v3 (unusable); Codecov bash uploader (removed) — the pinned
`codecov-action` is already correct.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode=auto`) + pytest-cov 7.1.0 (+ pytest-xdist 3.8.0 to add) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths=["tests"]`, `markers=["integration"]`) |
| Quick run command | `just test-bucket <name>` (per-bucket) / `uv run pytest tests/<bucket> -q` |
| Full suite command | `just integration-test` (ephemeral PG+Redis, auto-teardown) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CI-01 | Every collected test lives under exactly one known bucket dir | unit (partition-guard) | `uv run pytest tests/shared/test_partition_guard.py -x` | ❌ Wave 0 (D-06) |
| CI-01 | Each bucket is independently runnable | smoke | `just test-bucket discovery` … (one per bucket) | ❌ Wave 0 (recipe) |
| CI-02 | Buckets fan out in parallel; wall-clock drops | manual/CI-observed | `gh run view --json jobs` timing delta | manual (PR evidence) |
| CI-03 | Combined coverage total ≈ pre-reorg baseline; gate holds | integration | `just coverage-combine` (locally over shard files) → `coverage report --fail-under=85` | ❌ Wave 0 (recipe) |
| CI-03 | No file double-counted / dropped | assertion | combined total within ±tolerance of baseline ~90.38% | manual acceptance |
| CI-04 | Classifier maps doc-only change sets to `code-changed=false` | unit | `uv run pytest tests/<bucket>/test_change_gate.py -x` | ❌ Wave 0 (D-09) |
| CI-04 | Required check reports SUCCESS on a doc-only PR | manual | open a `.planning/**`-only PR; confirm `aggregate-results` green | manual |

### Sampling Rate
- **Per task commit:** `just test-bucket <affected-bucket>` (fast, targeted).
- **Per wave merge:** `just integration-test` (full suite, real services) — the reorg-safety net.
- **Phase gate:** full suite green + combined `coverage report --fail-under=85` green before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `tests/shared/test_partition_guard.py` — asserts every collected test is under a known bucket (CI-01, D-06).
- [ ] Change-gate classifier extracted to a tested script + `tests/<bucket>/test_change_gate.py` (CI-04, D-09).
- [ ] `just test-bucket` + `just coverage-combine` recipes (D-10) — the seams CI-01/03 validate against.
- [ ] `pytest-xdist` install (`uv add --dev pytest-xdist>=3.8.0`).
- [ ] `__init__.py` in each new bucket dir (import-mode=prepend requirement).
- [ ] Pre-reorg coverage baseline captured (`coverage report` total) to compare the combined number against.

## Security Domain

> `security_enforcement` not explicitly false → included. This phase changes CI/test layout only; no
> product code, endpoints, authN/Z, crypto, or input-validation surface changes.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no | — (no auth code touched) |
| V3 Session Mgmt | no | — |
| V4 Access Control | no | — |
| V5 Input Validation | no | — |
| V6 Cryptography | no | — |
| V14 Config / CI supply chain | **yes** | Frozen action SHAs (repo convention); `uv exclude-newer` cooldown for the new `pytest-xdist`; existing security.yml (pip-audit/bandit/semgrep/trivy/trufflehog) must keep passing under the broadened gate |

### Known Threat Patterns for CI changes
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Doc-only skip lets a code change bypass security scans | Tampering / EoP | Classifier must be *conservative* — anything not clearly a doc path stays `code-changed=true`; regression tests assert this (D-09) |
| Unpinned/typosquatted new action or package | Tampering | Freeze all action SHAs; `pytest-xdist` from pytest-dev; cooldown window |
| Secret exposure when moving Codecov upload | Info Disclosure | `CODECOV_TOKEN` only in the combine job; no token in per-bucket matrix legs |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `pytest-xdist>=3.8.0` is the right add; 3.8.0 clears the 7-day cooldown at lock time | Standard Stack | Lock fails → pin an older floor |
| A2 | `concurrency=["greenlet","thread"]` needs no `multiprocessing` add for xdist + pytest-cov | Code Examples §1 | If code-under-test spawns real subprocesses needing coverage, some lines under-report; verify combined total vs baseline |
| A3 | artifact actions v4+ are the versions to pin; exact current SHA to be resolved at plan time | Standard Stack | Wrong SHA → CI fails fast, easy fix |
| A4 | The branch-protection required check is (or should become) `aggregate-results` | Runtime State Inventory | If a matrix-leg name is required, doc-only PRs become unmergeable — maintainer must repoint |
| A5 | Keeping the integration bucket dir named `integration` + migrations under it preserves the auto-marker | Pitfall 3 | Silent loss of `integration` marker → wrong-job DB errors |

## Open Questions

1. **Q-A — "Unit buckets run service-free" (D-07) vs 55% DB penetration.**
   - What we know: 117/212 test files consume DB fixtures and are auto-marked `integration`; only 5
     live under `integration/`/`test_migrations/` today. Under a *directory* partition (D-03), those
     112 DB-backed files sit in domain buckets (review/analyze/identify/etc.), so those buckets are
     **not** service-free.
   - What's unclear: Whether to (a) provision Postgres+Redis in **every** bucket job (simplest,
     correct; "service-free" benefit dropped) — **recommended**; or (b) route all DB-backed tests to
     the integration bucket (guts the domain buckets — review/analyze would be nearly empty).
   - Recommendation: Plan for (a) and flag to the maintainer that D-07's "unit buckets run
     service-free … start instantly" clause is impractical given the fixture penetration; the
     dedicated `integration` bucket still exists but is not uniquely privileged in provisioning.
     **This likely warrants a CONTEXT revisit or an explicit planner note against D-07.**

2. **Q-B — `-n auto` inside every bucket (D-01) vs the shared-DB fixture race (Pitfall 4).**
   - What we know: `async_engine` does `create_all`/`drop_all` on one `TEST_DATABASE_URL`; xdist
     workers would race it.
   - What's unclear: Whether to run DB buckets serially (matrix-only parallelism — satisfies CI-02,
     safe) or build per-worker DB isolation to honor `-n auto` literally.
   - Recommendation: Ship serial DB buckets + `-n auto` on DB-free buckets first; treat per-worker DB
     isolation as an optional enhancement. Note against D-01 that literal "`-n auto` inside each
     bucket" is unsafe for DB buckets without isolation work.

3. **Q-C — Bucket assignment is manual/semantic.** Filename keywords ≠ bucket names ("discovery"=0
   files, "scan"=9). Someone must assign all 212 files to a bucket. Recommendation: produce an explicit
   file→bucket mapping table as a plan artifact; the partition-guard (D-06) enforces completeness.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| uv | all recipes | ✓ | (repo standard) | — |
| just | CI delegation | ✓ | via setup-just v4 pin | — |
| pytest / pytest-cov | test + coverage | ✓ | 9.1.1 / 7.1.0 | — |
| pytest-xdist | intra-bucket `-n` | ✗ | — (to add) | matrix-only parallelism (no xdist) still satisfies CI-02 |
| coverage.py | combine/xml/report | ✓ (via pytest-cov) | — | — |
| Postgres 18 / Redis 7 (CI services) | DB-backed buckets | ✓ (GH service containers) | 18-alpine / 7-alpine | — |
| Docker (local `just test-db`) | local integration run | ✓ (colima) | — | GH service containers in CI |

**Missing with no fallback:** none.
**Missing with fallback:** `pytest-xdist` — matrix fan-out alone satisfies CI-02 if xdist is deferred.

## Sources

### Primary (HIGH confidence)
- `coverage.readthedocs.io/en/latest/commands/cmd_combine.html` — `coverage combine`, `.coverage.*`
  discovery, `parallel`, `relative_files`, `[paths]`.
- Repo files (authoritative, this session): `.github/workflows/{ci,tests,code-quality,security}.yml`,
  `tests/conftest.py`, `tests/integration/conftest.py`, `pyproject.toml`, `justfile`,
  `.planning/{REQUIREMENTS,phases/63/63-CONTEXT}.md`.
- `pip index versions pytest-xdist` → 3.8.0 latest (verified 2026-07-02).

### Secondary (MEDIUM confidence)
- `hynek.me/articles/ditch-codecov-python` — canonical GH Actions matrix→combine pattern
  (`upload-artifact path: .coverage.*`, `include-hidden-files`, `download` `pattern:`+`merge-multiple:`,
  `coverage combine`/`report --fail-under`).
- `learn.scientific-python.org/development/guides/coverage/` — parallel-mode + combine guidance.
- coveragepy issue #2072 — `relative_files` + duplicate nested dir names caveat (low risk here).

### Tertiary (LOW confidence — verify at plan time)
- Exact current frozen SHAs for `actions/upload-artifact` / `actions/download-artifact` v4+.
- pytest-cov's precise interaction with `[run] parallel=true` under xdist (recommend the deterministic
  `COVERAGE_FILE=.coverage.<bucket>` approach to sidestep ambiguity).

## Metadata

**Confidence breakdown:**
- Coverage combine / xdist mechanics (CI-03): HIGH — official docs + canonical patterns + config read.
- Change-gate (CI-04): HIGH — 80% built, verified in `ci.yml`.
- Matrix/required-check topology (CI-02): HIGH — reusable-workflow result semantics verified against `ci.yml`.
- Directory reorg safety (CI-01): MEDIUM — hazards precisely identified (collisions, imports, marker,
  DB race), but the file→bucket mapping and the two D-tensions (Q-A/Q-B) need planner/maintainer calls.

**Research date:** 2026-07-02
**Valid until:** ~2026-08-01 (stable domain; artifact-action SHAs move fastest — re-verify pins at plan time)
