# Phase 63: Parallel CI & Code-Change Gating - Context

**Gathered:** 2026-07-02
**Status:** Ready for planning

<domain>
## Phase Boundary

Restructure the **existing CI** so the ~1,750-test pytest suite runs as parallel,
independently-selectable buckets with **one trustworthy combined coverage upload**, and
formalize the doc-only skip so heavy jobs don't run on documentation/planning changes.
All CI-workflow + test-layout work, **one PR**.

**No product/backend/pipeline behavior change** — this is CI/build/test-partition
infrastructure only (milestone framing: engineering-debt paydown). The "user" is the
project maintainer/operator, not the end user.

Requirements: CI-01, CI-02, CI-03, CI-04 (see `.planning/REQUIREMENTS.md`).

</domain>

<decisions>
## Implementation Decisions

### Sharding mechanism (CI-02)
- **D-01:** **Hybrid — GitHub job matrix over buckets, `pytest-xdist` (`-n auto`) inside each bucket.** The matrix gives CI-01 its literal "independently-runnable buckets" (each bucket is a separately visible CI job/check); xdist adds intra-bucket parallelism. Free runner-minutes on the public repo make the N-job fan-out cheap.
- **D-02:** Coverage is combined in **two stages**: xdist auto-combines within a job; the matrix jobs each emit a `.coverage.<bucket>` artifact, and a **final combine job** runs `coverage combine` + `coverage xml` → **single Codecov upload** (CI-03). This combine plumbing is exactly what CI-03 requires be trustworthy before Phase 64 raises the gate.

### Bucket selection (CI-01)
- **D-03:** **Directory reorg + path-glob selection.** Physically relocate `test_*.py` files into `tests/<bucket>/` directories; the matrix selects a bucket by its test path. Chosen over conftest auto-marking because a file living in exactly one directory makes the partition **structurally exclusive** — no test can land in zero or two buckets, so the CI-03 combined number is trustworthy by construction.
- **D-04:** **Buckets (from ROADMAP):** `discovery`, `metadata`, `fingerprint`, `analyze`, `identify` (identify/tracklist), `review` (review/apply), `agents` (agents/distributed), `integration` (real-Postgres/Redis), and `shared` (generic catch-all: schema, config, helpers, routing). The `shared` bucket is the **catch-all** for anything not matching a workflow-step.
- **D-05:** **Shared helpers and the root `conftest.py` stay at `tests/` root** — `conftest.py`, `_queue_fakes.py`, `_route_introspection.py`, `kube_fakes.py`, etc. pytest conftest scoping is hierarchical, so the root conftest still applies to every bucket subdir, and absolute imports (`from tests._queue_fakes import …`) keep working. **Only `test_*.py` files relocate.**
- **D-06:** Add a **partition-guard test** asserting every collected `test_*.py` resides under a known bucket directory (fails CI if a new test file is added outside the bucket dirs), closing the "silently unbucketed → dropped from coverage" gap. With directory-based buckets this guard is a path check, not a marker check.

### Integration tests placement
- **D-07:** **Dedicated `integration` bucket/job** owns `-m integration` (auto-marked by path today, per `tests/conftest.py:135`) and provisions the **postgres+redis service containers** — reusing the proven green setup in `.github/workflows/tests.yml`. Unit buckets run **service-free** and start instantly. The integration job's `.coverage` still folds into the combined report.

### Change-gate scope (CI-04)
- **D-08:** **Broaden the existing skip rule.** The current `detect-changes` job (in `.github/workflows/ci.yml`) already skips heavy jobs on `*.md`-only changes with **skip-with-success** via `aggregate-results`. Extend the "skippable" set to explicitly include `.planning/**`, `LICENSE`, and other non-source docs (not just `*.md`), and **document + lock the required-check contract** so a doc-only PR stays mergeable under branch protection (skip-with-success, never skip-absent).
- **D-09:** Keep the **changed-files gate job** approach (not bare `paths-ignore`) — this is what avoids the "required check never runs → PR can't merge" branch-protection trap. Add regression coverage for the gate logic.

### `just` delegation (project convention)
- **D-10:** All new CI steps **delegate to `just` recipes** (per [[feedback-workflows-use-just]]). Expect new/updated recipes such as a per-bucket test runner (e.g. `just test-bucket <name>`) and a `just coverage-combine`. Keep `justfile` and `scripts/update-project.sh` current with any new recipes.

### Claude's Discretion
- Exact matrix YAML shape, artifact names, `coverage combine` invocation, and whether the combine job also re-runs the Codecov action or a dedicated upload step.
- Whether `-n auto` vs a fixed worker count per bucket (tune to runner cores; `[tool.coverage.run] concurrency` already lists `greenlet, thread` — verify xdist's `multiprocessing` combine works with it).
- Exact `just` recipe names/signatures.
- Whether to keep a single `codecov` flag or per-bucket flags on the single combined upload (requirement is one upload; flags are cosmetic).

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements & roadmap
- `.planning/REQUIREMENTS.md` — CI-01..04 definitions + the milestone "No backend behavior change" framing and out-of-scope table.
- `.planning/ROADMAP.md` §"Phase 63" — goal, success criteria, and the "Resolve at planning" notes (marker-vs-dir-vs-xdist-vs-matrix, integration bucketing, changed-files gate).

### CI to restructure
- `.github/workflows/ci.yml` — top-level orchestrator; `detect-changes` (change-gating, lines ~33–80), `aggregate-results` (skip-with-success contract, lines ~111–158), job graph.
- `.github/workflows/tests.yml` — the reusable test workflow: postgres/redis services, migrations DB creation, `just install` + `just test-ci`, Codecov upload (the thing being parallelized).
- `.github/workflows/code-quality.yml`, `security.yml`, `docker-validate.yml` — sibling reusable workflows gated by `detect-changes`; the broadened skip rule must keep their required-check contract intact.

### Test suite & config
- `tests/conftest.py` §~129–135 — existing path-based auto-marking of `integration` (the precedent for how markers are applied); root conftest that must stay at `tests/` root.
- `tests/integration/` (+ its `conftest.py`) — the real-Postgres tests destined for the dedicated integration bucket.
- `pyproject.toml` §`[tool.pytest.ini_options]` (markers, `testpaths`), §`[tool.coverage.report]` (`fail_under = 85`), §`[tool.coverage.run]` (`source`, `omit`, `concurrency`).
- `justfile` §`[group('test')]` — existing `test`, `test-cov`, `test-ci`, `test-file`, `test-db`, `integration-test` recipes to extend.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`detect-changes` + `aggregate-results` (`ci.yml`)**: CI-04 is ~80% built. `detect-changes` computes `code-changed` from a `git diff` of changed files (handles PR / new-branch / force-push SHA edge cases); `aggregate-results` reports **success** when docs-only jobs are skipped. Phase work = broaden the file classifier and add tests, not build from scratch.
- **`tests.yml` service block**: postgres:18-alpine + redis:7-alpine with healthchecks, migrations-DB creation, and the exact env (`DATABASE_URL`, `TEST_DATABASE_URL`, `PHAZE_REDIS_URL`) the integration tests need — lift into the dedicated integration bucket verbatim.
- **`tests/conftest.py` path auto-marker**: applies `pytest.mark.integration` by path — the established pattern; directory reorg makes bucket assignment even simpler (path == bucket).
- **`just test-ci`**: already emits `coverage.xml` for Codecov; the combine job produces the same artifact shape from combined shards.

### Established Patterns
- Reusable workflows via `workflow_call`; emoji-prefixed step names; frozen action SHAs; CI delegates to `just`.
- Concurrency group with `cancel-in-progress` on PRs (`ci.yml:20-22`) — matrix must not break this.
- `fail_under = 85` in `[tool.coverage.report]` today; Phase 64 raises it — **Phase 63 must not lose per-shard coverage** or Phase 64's gate sits on a wrong number.

### Integration Points
- Codecov upload currently in `tests.yml`; moves to the final combine job (single upload preserved).
- Branch-protection required-check names may change when the single `test` job becomes a matrix + combine — the required check should point at a **stable aggregate/combine job**, not per-bucket jobs, so doc-only skip-with-success and the coverage gate both stay satisfiable.

</code_context>

<specifics>
## Specific Ideas

- Directory reorg is the one invasive move; keep it mechanical (git mv `test_*.py` into `tests/<bucket>/`, leave shared helpers + root conftest put) and verify the full suite is green **before** wiring the matrix, so a test regression can't hide behind a CI change.
- Prefer the required/branch-protection status check to be the **combine/aggregate job** so both the coverage gate and the doc-only skip-with-success remain satisfiable from one stable check name.

</specifics>

<deferred>
## Deferred Ideas

- **Coverage gate raise + per-module uplift** → Phase 64 (COV-01/02). Phase 63 only makes the combined number *correct*, it does not raise the gate.
- **REQUIREMENTS traceability CI gate, `/saq` re-link, dead-code sweep** → Phase 66 (DOCS-01, CLEAN-01/02). The traceability gate will slot into this restructured CI later.
- **pytest-cov → other coverage tooling swap** → explicitly out of scope (REQUIREMENTS out-of-scope table).

None else — discussion stayed within phase scope.

</deferred>

---

*Phase: 63-parallel-ci-code-change-gating*
*Context gathered: 2026-07-02*
