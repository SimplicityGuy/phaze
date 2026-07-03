# Phase 64: Per-Module Coverage Uplift & Gate Raise - Research

**Researched:** 2026-07-02
**Domain:** Python test coverage enforcement (coverage.py 7.14.2 + pytest-cov 7.1.0), CI gate wiring, behavior-asserting tests for FastAPI async / SQLAlchemy async / SAQ
**Confidence:** HIGH (every coverage number below was measured this session by running the real suite against a live test Postgres/Redis)

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Two guardrails — global `fail_under` bump (COV-02) AND a per-module floor check (COV-01). Both enforced. Rationale: a high aggregate can hide one rotting module.
- **D-02:** Per-module floor enforced by a **small script** (coverage.py has no native per-file `fail_under`). Parses coverage data (e.g. `coverage json`), fails on any tracked module below the floor. Runs in the **combine step** (authoritative combined `.coverage` — Phase 63 D-02), delegated via a **`just` recipe**. Policy is fixed (custom per-module check over combined data); exact shape is Claude's discretion.
- **D-03:** Single uniform floor — no per-module ratchet / recorded-baseline map. A module passes or is explicitly exempted.
- **D-04:** Uniform per-module floor = **85%**. Every tracked module ≥ 85% or explicitly exempted.
- **D-05:** Global gate = **measured post-uplift overall minus ~1 point**. Do the uplift, measure achieved overall, set `fail_under` ~1 point below. **Must be strictly > 90.38 and target low-90s-or-higher.** Exact number pinned at execute time from the measured number.
- **D-06:** The 85% per-module floor **IS the scope** — every tracked module reaches 85% or is exempted, not just the ~6 named offenders. Prioritize v7.0-touched + worst offenders first, then the tail. *(NOTE: see the Re-Baselining section — the named-offender percentages are a measurement artifact; the true combined map is very different and dramatically narrows the actual uplift work.)*
- **D-07:** **Behavior-asserting quality bar.** Every added test asserts an observable outcome (return value, DB/ORM state, HTTP status/body, emitted log/side-effect). No "call it and assert no exception" coverage-padding. Reviewer/verifier flags padding as a defect.
- **D-08:** **Behavior-preserving testability seams allowed** (extract pure fn, inject clock/dependency, split loop body) ONLY when runtime behavior is provably unchanged; verifier confirms zero delta (git-diff-level reasoning). Sanctioned exception to the "no backend behavior change" rule.
- **D-09:** **Exemptions (`# pragma: no cover` / `omit`) require inline written justification** explaining why the code is genuinely untestable. Should be rare given D-08.

### Claude's Discretion
- Exact per-module-floor script implementation (language/shape), coverage data format (`coverage json` vs parsing `.coverage`), and whether an off-the-shelf tool satisfies it — as long as it enforces D-01/D-02/D-03 over the combined coverage.
- The exact `just` recipe name/signature (fold into `coverage-combine` vs a separate `coverage-floor` recipe).
- The precise final `fail_under` number (D-05: measured minus ~1).
- Which lines qualify for D-09 exemptions vs D-08 seams — decided per module during execution, reviewer-confirmed.
- Per-test-file organization within the Phase 63 bucket dirs (`tests/<bucket>/`).

### Deferred Ideas (OUT OF SCOPE)
- None. (Codecov project/patch target retuning is advisory-only, folded into D-05's "verify alignment" note — not a separate deliverable. No codecov.yml exists today; see Gate Wiring.)
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| COV-01 | Raise under-covered modules to a per-module floor (prioritize v7.0-touched + worst offenders), tests asserting observable behavior | Authoritative combined map (below) shows the real floor gap is tiny — only `services/review.py` (83.16%) is below 85%. Behavior-asserting test patterns + reusable fixtures documented in Testing the Modules. |
| COV-02 | Raise the enforced coverage gate above the 90.38% baseline and wire it into CI so future regressions fail the build | Two edit sites identified (`pyproject.toml` + `justfile coverage-combine`); measured combined overall = **96.89%** → gate ~1 below (D-05). Per-module floor script + `just` recipe + CI combine-job step designed below. |
</phase_requirements>

## Summary

This phase has two deliverables: (1) a **per-module coverage floor** enforced in CI (the real net-new engineering — coverage.py has no native per-file `fail_under`), and (2) a **global gate raise** above the 90.38% baseline. Both run against the **combined** coverage the Phase 63 `combine` job produces, never per-bucket shards (a shard only exercises a fraction of `phaze` and is meaningless per-module).

**The single most important finding — re-baselining (research Q5).** The "worst offender" percentages in CONTEXT/PROJECT.md (`agent_liveness.py 12.5%`, `shell.py 39.7%`, `pipeline.py 65.5%`, `~69%` routers, `71–78%` tail) are a **measurement artifact of a no-database run**, not the authoritative combined coverage. I measured both this session against a live test Postgres+Redis:

- **Full combined suite (2566 tests, all DB tests included): overall = 96.89%.** Exactly ONE module is below the 85% floor: `services/review.py` at **83.16%** (needs ~2 more lines covered).
- **`pytest -m "not integration"` (no DB, 1594 tests, 992 deselected): overall = 68.16%** — and it reproduces the low offender numbers (`shell.py 33.33%`, `routers/pipeline.py 23.08%`, `services/review.py 24.21%`, `services/pipeline.py 61.86%`, `routers/tracklists.py 17.83%`, `services/agent_liveness.py 68.75%`, `main.py 100%`). This is where the CONTEXT numbers came from.

The named offenders are **already well above 85%** in the real combined measurement because their behavior is exercised by DB-fixture (integration-marked) tests. **The planner must not schedule hundreds of redundant tests for modules that are already covered.** The actual per-module uplift is small; the bulk of the phase's engineering value is the floor-enforcement machinery and setting a defensible gate number.

**Primary recommendation:** Build a small stdlib-only Python script (`scripts/coverage_floor.py`, zero new dependencies) that reads `coverage json` output and fails if any tracked `phaze/**` module's `summary.percent_covered` < 85; wire it into `just coverage-combine` after `coverage combine` (so it runs on combined data in the Phase 63 combine job). Raise `services/review.py` to ≥85% with behavior-asserting formatting/degrade-path tests, add margin to the four 85–90% modules, then set the global `fail_under` to the freshly-measured combined overall minus ~1 point (≈95, strictly > 90.38).

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Per-module floor enforcement | CI / build tooling (`scripts/` + `justfile` + `tests.yml combine` job) | — | Runs post-`coverage combine`; pure data-parsing, no app runtime involvement |
| Global gate enforcement | CI / build tooling (`pyproject.toml` + `justfile`) | — | Two config edit sites; enforced once on combined number |
| Behavior-asserting tests | Test suite (`tests/<bucket>/`) | App code (D-08 seams only) | New tests land in Phase 63 bucket dirs; app code changes only for provable-neutral seams |
| Testability seams (D-08) | App code (`src/phaze/**`) | Verifier (git-diff review) | Behavior-preserving refactors gated by zero-delta confirmation |

**No product/runtime tier is touched.** This is test + CI/coverage-config work (milestone hard constraint). D-08 seams are the only sanctioned `src/phaze/**` edits and must be behavior-neutral.

## Re-Baselining: The Authoritative Per-Module Coverage Map

> This is the table the planner needs to scope COV-01 correctly. **Measured 2026-07-02**, full suite against live Postgres (`postgres:18-alpine`, port 5433) + Redis (port 6380), single process = union of all bucket coverage. Overall matches Phase 63's reported combined `96.89%` exactly, confirming this ≈ the CI combine result. `[VERIFIED: local coverage run]`

**Overall combined: 96.89%** across 154 source files.

### Modules BELOW the 85% floor (the entire COV-01 uplift target)

| Module | Combined % | Stmts | Miss | Lines to cover to reach 85% | Nature of the gap |
|--------|-----------|-------|------|-----------------------------|-------------------|
| `services/review.py` | **83.16%** | 95 | 16 | **2** (need miss ≤ 14) | Missing lines are (a) degrade-safe `except Exception → return []` branches (74–76, 134–136, 197–199, 267–269) and (b) pure formatter branches in `_format_size`/`_format_quality` (142, 148, 156). All trivially behavior-assertable. |

**That is the complete list.** No 71–78% tail exists in the combined measurement.

### Modules in the 85.00–89.99% band (already pass the floor; candidates for margin per D-05)

| Module | Combined % | Stmts | Miss | Notes |
|--------|-----------|-------|------|-------|
| `services/agent_liveness.py` | 85.42% | 48 | 7 | Sits exactly on the floor. Missing lines 174–180 = the `SQLAlchemyError` degrade path in `classify_compute_lanes` (rollback → `("IDLE", 0)`). One test injecting a raising session covers all 7 and buys margin. |
| `services/agent_client.py` | 86.72% | 128 | 17 | HTTP error/timeout branches (missing 340–459 cluster). |
| `routers/tags.py` | 89.72% | 214 | 22 | Router edge branches. |
| `tasks/tracklist.py` | 89.92% | 129 | 13 | Task error/skip branches. |

All other 149 files are ≥ 90%.

### How the discrepancy arose (so the planner can trust the 96.89% number)

- The Phase 63 `combine` job unions all 9 buckets' `.coverage.<bucket>` shards, **including the `integration` bucket and every DB-fixture test spread across buckets** (Phase 63 D-07: 55% of test files consume DB fixtures). Those tests exercise the router/service DB paths → high per-module coverage.
- Measuring with `-m "not integration"` (or with no DB up) deselects ~992 tests → the DB-exercised lines show as uncovered → the low "offender" numbers. That is NOT what CI enforces.
- **The gate and the floor must both run on the combined number** (D-02 / Phase 63 D-02). My single-process full-suite run is the correct baseline; the planner should reproduce it the CI way to pin the final gate digit (commands below).

### Commands to reproduce the authoritative map (for the planner, at execute time)

Fastest local path (single process, ≈ combined — what produced the numbers above):
```bash
just test-db   # ephemeral Postgres:5433 + Redis:6380
export TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_test"
export MIGRATIONS_TEST_DATABASE_URL="postgresql+asyncpg://phaze:phaze@localhost:5433/phaze_migrations_test"
export DATABASE_URL="$TEST_DATABASE_URL"
export PHAZE_REDIS_URL="redis://localhost:6380/0"
uv run pytest tests/ -q --cov=phaze --cov-report= --cov-fail-under=0
uv run coverage json -o coverage.json
uv run coverage report --sort=cover        # human-readable per-module table, ascending
just test-db-down
```
CI-faithful path (per-bucket shards → `coverage combine`, exactly mirrors the combine job):
```bash
for b in discovery metadata fingerprint analyze identify review agents integration shared; do
  COVERAGE_FILE=.coverage.$b uv run pytest tests/$b --cov=phaze --cov-report= --cov-fail-under=0 -q
done
uv run coverage combine        # merges .coverage.* into .coverage
uv run coverage json -o coverage.json
uv run coverage report --sort=cover
```
Note: full suite ≈ 9m22s locally under colima; single-process is fine for the map, CI-faithful is only needed to pin the exact final `fail_under` digit.

## Per-Module Floor Enforcement Mechanism (research Q1)

### Recommendation: a stdlib-only Python script over `coverage json`. No new dependency.

**Verified facts about coverage.py 7.14.2** `[VERIFIED: import coverage; __version__]` `[CITED: coveragepy/coveragepy docs]`:
- `[tool.coverage.report] fail_under` and `coverage report --fail-under` / `coverage json --fail-under` are **total-only**. There is **no native per-file / per-module `fail_under`.** (Confirmed against current coverage.py docs — no per-file threshold feature exists in 7.x.)
- `coverage json -o coverage.json` emits a top-level `files` dict keyed by relative path (with `relative_files = true`, already set — Phase 63 D added it), each with a `summary` object containing `percent_covered` (float) and `percent_covered_display` (string). Structure `[CITED: coveragepy/coveragepy docs, JSON Report Summary Structure]`:
  ```json
  {"files": {"src/phaze/services/review.py": {"summary": {"num_statements": 95, "missing_lines": 16, "percent_covered": 83.16, ...}}}, "totals": {"percent_covered": 96.89, ...}}
  ```

**Why a custom script (not an off-the-shelf tool):**
- `diff-cover` measures coverage of the *diff* (changed lines), not a per-file floor over the whole tree — wrong tool for D-01/D-03. `[ASSUMED]`
- `coverage-conditional-plugin` toggles config by environment, unrelated to per-file thresholds. `[ASSUMED]`
- `pytest-cov` exposes only `--cov-fail-under` (total). No per-module option. `[VERIFIED: pytest-cov 7.1.0 in project]`
- Adding any package is friction the project actively avoids: the 7-day `[tool.uv] exclude-newer` supply-chain cooldown, slopcheck-style vetting, litellm-incident scarring. A ~40-line stdlib script (`json` + `sys` + `fnmatch`) is zero-dependency, auditable, and matches the project's `scripts/classify-changed-files.sh` precedent (Phase 63 extracted CI logic to a testable script). **Strongly recommended.**

**Recommended script shape** (`scripts/coverage_floor.py`, Python 3.14, ruff/mypy-clean, typed):
```python
"""Fail if any tracked phaze module is below the per-module coverage floor (COV-01, D-01/D-02/D-03).

Reads `coverage json` output and enforces a single uniform floor (D-04=85) over every
tracked source file. Runs in `just coverage-combine` AFTER `coverage combine`, so it sees
the authoritative COMBINED coverage (Phase 63 D-02) — never a partial per-bucket shard.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


FLOOR = 85.0
# D-09 exemptions: {relative_path: "written justification"}. Keep empty unless a module is
# genuinely untestable AND D-08 seams cannot help. Reviewer must confirm each entry.
EXEMPT: dict[str, str] = {}


def main() -> int:
    data = json.loads(Path("coverage.json").read_text(encoding="utf-8"))
    failures: list[tuple[str, float]] = []
    for path, info in sorted(data["files"].items()):
        if path in EXEMPT:
            continue
        if info["summary"]["num_statements"] == 0:  # __init__.py / empty modules
            continue
        pct = info["summary"]["percent_covered"]
        if pct < FLOOR:
            failures.append((path, pct))
    if failures:
        print(f"❌ Per-module coverage floor {FLOOR:.0f}% not met:")  # noqa: T201
        for path, pct in failures:
            print(f"   {pct:6.2f}%  {path}")  # noqa: T201
        return 1
    print(f"✅ All tracked modules ≥ {FLOOR:.0f}% (combined coverage).")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
```
Invocation inside the combine recipe (needs `coverage json` first — `coverage-combine` currently emits only `coverage xml`):
```make
coverage-combine:
    uv run coverage combine
    uv run coverage xml
    uv run coverage json                       # NEW: per-module data for the floor check
    uv run coverage report --fail-under=<NEW_GLOBAL>   # global gate (D-05, raise from 85)
    uv run python scripts/coverage_floor.py    # NEW: per-module floor (D-01/D-02)
```
Discretion (D-02): a dedicated `just coverage-floor` recipe called from `coverage-combine` is equally valid and arguably cleaner (keeps the recipe declarative and independently runnable). Either satisfies "delegated via a `just` recipe."

## Tracked-Module Set & Exemptions (research Q2)

- **Tracked set = every file in `coverage json`'s `files` dict** (which is exactly `source = ["phaze"]` from `[tool.coverage.run]`, minus `omit = ["tests/*"]`). Iterating the JSON `files` keys means the tracked set is **self-maintaining** — a new `phaze/**` module automatically appears and is subject to the floor. No hand-maintained module list to drift. Recommended over an explicit allowlist (D-03 "simpler to state and enforce").
- **Skip zero-statement files** (`num_statements == 0`) — `__init__.py`, pure re-export modules. coverage reports these at 100% or 0% inconsistently; excluding them avoids false failures without hiding real code.
- **Exemptions (D-09) interaction:**
  - `# pragma: no cover` — removes lines from the denominator *before* the script runs (coverage already excludes them). Best for a handful of genuinely-untestable lines (e.g. `if __name__ == "__main__":`). Requires an inline justification comment (D-09).
  - `[tool.coverage.run] omit` — removes a whole file from the `files` dict; the script never sees it. Use only for entire un-testable modules; carries the same written-justification requirement.
  - **Script-level `EXEMPT` dict** — keeps a module *in* the coverage report (so its real number is visible in Codecov) but exempts it from the *floor gate*, with the justification stored right next to the check. Recommended for the rare "visible but exempted" case because the justification lives in one auditable place. Given D-08 seams are allowed, expect `EXEMPT` to stay empty.
- **Precision note:** `[tool.coverage.report] precision = 2` is set; the script compares the float `percent_covered`, so 84.995% correctly fails an 85.0 floor. Keep the script's comparison on the raw float, not `percent_covered_display` (which rounds).

## Testing the Modules (research Q3)

The project has a mature async test harness. **Use it — do not hand-roll app bootstrapping.** All patterns below are `[VERIFIED: tests/conftest.py + existing bucket tests]`.

### Reusable fixtures (in `tests/conftest.py`, apply to every bucket via hierarchical conftest scoping)
| Fixture | Yields | Use for |
|---------|--------|---------|
| `session` | `AsyncSession` on the test DB (tables created/dropped per test via `Base.metadata`) | Service-layer tests that read/write ORM state |
| `client` | `httpx.AsyncClient` over `ASGITransport(create_app())` with `get_session` overridden to the test session | Router/HTMX endpoint tests |
| `authenticated_client` | `client` + `Authorization: Bearer <token>` for `Depends(get_authenticated_agent)` routes | Agent-facing routers |
| `make_file` | factory → persisted `FileRecord` | Seed a file in any state |
| `seed_pending_proposal`, `seed_executed_file_with_metadata`, `seed_duplicate_group`, `seed_cue_set`, `seed_file_with_windows`, `seed_distinct_artists`, `seed_cloud_jobs` | domain-specific seeded graphs | Review/dedupe/cue/analysis tests — **directly relevant to `review.py`** |
| `job_env`, `kube_respx`, `kube_fakes` | one-shot job + fake kube/http | Agent/cloud tests |

### `services/review.py` (the only sub-floor module) — the concrete COV-01 target
Pattern to replicate: **`tests/shared/services/test_pipeline.py`** (service functions exercised through the `session` fixture with seeded rows) and **`tests/shared/routers/test_pipeline.py`** (router paths via `client`).
- **Degrade-path branches (74–76, 134–136, 197–199, 267–269):** each is an `except Exception → logger.warning(...) → return []`. D-07-compliant test: pass a session/dependency that raises (e.g. a `SimpleNamespace` or a mock whose `.execute`/`.begin_nested` raises), then assert (a) the return value is `[]` AND (b) the warning was logged via `caplog` (the autouse `_route_structlog_through_stdlib` fixture routes structlog → stdlib so `caplog` captures it). This asserts an observable outcome (graceful degrade), not just "no exception" — squarely within D-07.
- **Pure formatter branches (`_format_size` 142/148, `_format_quality` 156):** call `_format_size(None) == "unknown size"`, `_format_size(2**60)` → the `"… PB"` branch, `_format_quality({"bitrate": 320, "file_size": …})` → `"320 kbps · …"`. Trivial return-value assertions. No seam needed.

### `services/agent_liveness.py` (85.42%, on the floor — add margin)
- This module is **pure functions + one degrade-safe DB read** — NOT a background heartbeat. **The CONTEXT description ("background asyncio heartbeat task") is inaccurate for this file.** The Phase-46 asyncio heartbeat lives in `tasks/agent_worker.py` (launched at worker startup, cancelled at shutdown) with a shim in `tasks/heartbeat.py`; it is a different module and not sub-floor. `[VERIFIED: read src/phaze/services/agent_liveness.py + STATE.md Phase 46 note]`
- Missing lines 174–180 = the `SQLAlchemyError` branch in `classify_compute_lanes` (rollback → `("IDLE", 0)`). Test: seed via `seed_cloud_jobs` for the happy paths (already covered), then inject a session whose `.execute` raises `SQLAlchemyError` and assert the tuple is `("IDLE", 0)` AND the degrade warning logged. `classify`/`sort_key` are already fully covered by `tests/agents/services/test_agent_liveness.py`.

### `main.py` (100% in the combined run — NOT a target)
- `main.py` reports **100%** combined. The `create_app()` factory + `lifespan` are exercised by every `client`-fixture test (the ASGI app is built per test). No work needed. If margin-building elsewhere ever dips it, the `lifespan` startup/shutdown wiring is the only genuinely-bootstrap-flavored code and would be a legitimate D-09 candidate — but it is not needed now.

### `routers/shell.py`, `routers/pipeline.py`, `routers/tracklists.py`, `services/pipeline.py` (all named offenders — all already ≥90% combined)
- These are **not** below the floor in the authoritative measurement (they were only low in the no-DB run). Existing coverage: `tests/shared/core/test_shell_routes.py` (shell via `client`), `tests/shared/routers/test_pipeline*.py`, `tests/shared/services/test_pipeline.py`, `tests/identify/routers/test_tracklists.py`. **No new tests required for COV-01.** If the planner wants defensive margin under the raised global gate, target their specific missing branches (get exact lines from `coverage report -m`), but scope this as optional margin, not floor-clearing.
- HTMX endpoint pattern (for any router margin work): `resp = await client.get("/s/analyze")` then assert `resp.status_code == 200` and assert on rendered-fragment markers (e.g. `'id="stage-workspace"' in resp.text`, `'data-stage="analyze"' in resp.text`) — exactly as `test_shell_routes.py` does. Assert the *rendered fragment content*, not just status (D-07).

### D-08 seams — likely unnecessary
Because the real gap is `review.py`'s already-testable degrade/formatter branches, **no testability seam is needed to clear the floor.** Reserve D-08 for any margin-building on a genuinely-hard branch, and only with verifier zero-delta confirmation.

## Gate Wiring (research Q4)

### The two edit sites for the global gate (D-05) — keep consistent
1. **`pyproject.toml` `[tool.coverage.report] fail_under = 85`** (line 68) → raise to `<NEW_GLOBAL>`.
2. **`justfile` `coverage-combine`** (line 110): `uv run coverage report --fail-under=85` → `--fail-under=<NEW_GLOBAL>`.
   - `--fail-under` on the CLI overrides the config value, so both must move together or the CLI silently wins. A test asserting the two numbers match is cheap insurance (mirror `tests/shared/core/*` guard-test style).
3. **`justfile` `test-bucket`** (line 103) uses `--cov-fail-under=0` **on purpose** (a shard is partial) — **do NOT change it.** The gate is enforced once, on the combined number, in `coverage-combine`. `[VERIFIED: justfile comment lines 95–99]`

### Where the per-module floor check lands in CI
- **`.github/workflows/tests.yml` `combine` job**, step "🧮 Combine coverage and enforce gate" (line 144–145) runs `just coverage-combine`. Because the new floor check is *inside* `coverage-combine`, **no workflow YAML edit is needed** — the floor runs automatically in the combine job, on the combined `.coverage`, before the Codecov upload. This is the correct place (authoritative combined data lives only here — Phase 63 D-02). `[VERIFIED: read tests.yml]`
- **How a failure surfaces as a red required check:** the `combine` job is the branch-protection required check (Phase 63 D: required check points at the stable combine/aggregate job, not per-bucket jobs). `just coverage-combine` exits non-zero when either the global `coverage report --fail-under` fails (exit 2) or `scripts/coverage_floor.py` fails (exit 1) → the combine job fails → the required check is red → PR blocked. Exactly the regression trap COV-02 wants. `[VERIFIED: tests.yml combine job + Phase 63 D / ci.yml aggregate-results]`

### Setting `<NEW_GLOBAL>` (D-05)
- Measured combined overall today = **96.89%**. D-05 = achieved-minus-~1. After the small `review.py` uplift the overall will be ≥ 96.89%. A gate of **95** (or 96) is strictly > 90.38, lands in the "low-90s-or-higher" target, and leaves ~1.5–2 points of headroom so unrelated future PRs are not brittle-blocked. **Pin the exact digit at execute time from the freshly-measured post-uplift overall** (D-05 mandates measure-then-set). Recommend an integer to avoid `precision`-vs-`fail_under` float edge cases.
- **Codecov alignment (D-05 advisory note):** there is **no `codecov.yml` in the repo** `[VERIFIED: ls]` — Codecov runs on its defaults (auto project target = previous coverage). CLAUDE.md describes intended Codecov targets but no file enforces them. Codecov is advisory; the CI hard gate is `just coverage-combine`. No Codecov change is required this phase; if the operator later wants the documented project/patch targets, that is a separate (out-of-scope) file addition.

## Standard Stack

**No new packages.** This phase adds a stdlib-only script and edits config/tests. The existing toolchain is sufficient and current:

| Tool | Version (verified this session) | Role |
|------|----------|------|
| coverage.py | 7.14.2 | `coverage combine` / `json` / `report` / `xml` — the data source and global gate |
| pytest | 9.1.1 | test runner |
| pytest-cov | 7.1.0 | `--cov` collection during pytest |
| pytest-asyncio | 1.4.0 (`asyncio_mode = auto`) | async test support |
| httpx | 0.28.1 (`ASGITransport` + `AsyncClient`) | in-process FastAPI endpoint tests |
| pytest-xdist | 3.8.0 | present; irrelevant to floor work (DB buckets run serial) |

`[VERIFIED: uv run python -c "import coverage; ..." + pyproject dependency-groups]`

**Installation:** none.

## Package Legitimacy Audit

**No external packages are installed by this phase.** The per-module floor is a stdlib-only script (`json`, `sys`, `pathlib`). No slopcheck/registry verification applies. `[VERIFIED: recommendation is zero-dependency]`

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-file coverage from raw data | A parser over the binary `.coverage` SQLite file | `coverage json` → parse `files[*].summary.percent_covered` | Documented stable JSON schema; binary format is internal/unstable |
| FastAPI app under test | Manual ASGI/uvicorn spin-up | `client` / `authenticated_client` fixtures (`ASGITransport`) | Already wired with DB-session override + auth |
| Test DB lifecycle | Manual `CREATE/DROP` SQL | `session` fixture + `just test-db` | Per-test `Base.metadata.create_all/drop_all` + legacy-agent seed already handled |
| Capturing structlog in tests | Reconfiguring logging per test | autouse `_route_structlog_through_stdlib` + `caplog` | Already routes structlog → stdlib so `caplog` works |
| Diff/patch coverage gate | `diff-cover` | (not needed) | This phase enforces an absolute per-file floor (D-03), not a diff floor |

**Key insight:** the entire per-module mechanism is a thin, testable parse over an official, stable JSON schema. Every "hard" module is already testable through the existing async harness — the perceived difficulty was a measurement artifact, not real test-infrastructure gaps.

## Common Pitfalls

### Pitfall 1: Enforcing the floor on a per-bucket shard
**What goes wrong:** running the floor check inside `test-bucket` (or on a single `.coverage.<bucket>`) fails every module, because a shard exercises only a fraction of `phaze`.
**How to avoid:** the check runs ONLY in `coverage-combine`, after `coverage combine` unions all shards. `test-bucket` keeps `--cov-fail-under=0`. (Phase 63 D-02; justfile comment.)

### Pitfall 2: Trusting the stale "worst offender" numbers
**What goes wrong:** the planner schedules large test-writing waves for `shell.py`/`pipeline.py`/`tracklists.py` believing they're at 30–70%, when combined they're ≥90%. Wasted effort + risk of coverage-padding to hit an already-met bar.
**How to avoid:** re-baseline first (commands above). The real sub-floor set is `{services/review.py}`.

### Pitfall 3: `--cov-fail-under` (CLI) silently overriding `fail_under` (config)
**What goes wrong:** raising `pyproject.toml fail_under` but not the `justfile` `--fail-under=` (or vice versa) → the CLI value wins in CI and the two drift.
**How to avoid:** edit both sites; add a guard test asserting they match.

### Pitfall 4: Coverage-padding to clear the floor (D-07 violation)
**What goes wrong:** `await get_dedupe_groups(session)` with no assertion, or asserting only "no exception."
**How to avoid:** every new test asserts an observable outcome — return value, ORM state, HTTP body/status, or a `caplog` log record. For degrade branches, assert BOTH the `[]`/default return AND the emitted warning.

### Pitfall 5: `get_settings` lru_cache / `PHAZE_ROLE` leakage across tests
**What goes wrong:** a test that imports agent-role code (or sets `PHAZE_ROLE=agent`) poisons the cached settings singleton for later tests — a real, documented hazard surfaced by the Phase 63 bucket split.
**How to avoid:** the autouse `_isolate_pydantic_settings_from_env_file` fixture already `cache_clear()`s per test; new tests must not defeat it (don't cache settings module-globally). New tests must also pass in *isolation* via `just test-bucket <bucket>`, not only in the full suite (see MEMORY: CI bucket test-isolation).

### Pitfall 6: New test file placed outside a bucket dir
**What goes wrong:** a `test_*.py` (or `*_test.py`) added at `tests/` root or a non-bucket path is dropped from the matrix → not in combined coverage → the floor/gate silently sits on wrong data.
**How to avoid:** the Phase 63 D-06 partition-guard test fails CI on any test file outside a known bucket dir. Place new tests under the correct `tests/<bucket>/` (review.py tests → `tests/review/`, agent_liveness → `tests/agents/`).

## Code Examples

### Behavior-asserting degrade-path test (the review.py pattern)
```python
# tests/review/services/test_review_degrade.py  (bucket: review)
# Source: pattern from tests/shared/services/test_pipeline.py + conftest _route_structlog_through_stdlib
import logging
from types import SimpleNamespace

import pytest

from phaze.services.review import get_dedupe_groups


@pytest.mark.asyncio
async def test_get_dedupe_groups_degrades_to_empty_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Observable outcome: a DB failure yields [] AND emits the degrade warning (D-07)."""
    class _Boom:
        async def begin_nested(self):  # noqa: ANN202
            raise RuntimeError("db down")

    with caplog.at_level(logging.WARNING):
        result = await get_dedupe_groups(_Boom())  # type: ignore[arg-type]

    assert result == []                                   # observable return value
    assert any("dedupe_groups_degraded" in r.message or "dedupe_groups_degraded" in r.getMessage()
               for r in caplog.records)                   # observable side-effect (log)
```

### Pure-formatter test (return-value assertion)
```python
# Source: src/phaze/services/review.py _format_size / _format_quality
from phaze.services.review import _format_quality, _format_size


def test_format_size_edges() -> None:
    assert _format_size(None) == "unknown size"
    assert _format_size(0) == "unknown size"
    assert _format_size(22_400_000).endswith(" MB")
    assert _format_size(2**60).endswith(" PB")          # exercises the loop-exhaustion branch


def test_format_quality_with_and_without_bitrate() -> None:
    assert _format_quality({"file_size": 22_400_000, "bitrate": 320}).startswith("320 kbps · ")
    assert "kbps" not in _format_quality({"file_size": 22_400_000})
```

### The floor script (see full version in the Mechanism section)
Reads `coverage.json` `files[*].summary.percent_covered`, exits 1 listing any tracked module < 85. Zero dependencies.

## State of the Art

| Old Approach | Current Approach | Impact |
|--------------|------------------|--------|
| Single global `fail_under` on one serial coverage run | Per-shard `.coverage` → `coverage combine` → single gate (Phase 63) | The floor/gate must key off the COMBINED number; per-bucket enforcement is meaningless |
| Believe the PROJECT.md offender table | Re-measure combined before scoping | Offender table was a no-DB artifact; combined is 96.89% |

**Deprecated/outdated:** the CONTEXT/PROJECT.md per-module offender percentages (agent_liveness 12.5%, shell 39.7%, pipeline 65.5%, ~69% routers, 71–78% tail) — superseded by the authoritative combined map. The `agent_liveness.py` "background asyncio heartbeat" description is also inaccurate (that code is in `tasks/agent_worker.py`).

## Runtime State Inventory

Not applicable — this is a test + CI-config phase with no rename/refactor/migration and no runtime/stored state. The only `src/phaze/**` edits permitted are D-08 behavior-neutral seams (likely none needed). **None — verified by scope (tests + `pyproject.toml`/`justfile`/`scripts/` only).**

## Validation Architecture

> `workflow.nyquist_validation` — `.planning/config.json` not inspected for an explicit `false`; treated as enabled.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.1.1 + pytest-asyncio 1.4.0 (`asyncio_mode = auto`) + pytest-cov 7.1.0 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`testpaths = ["tests"]`, `integration` marker) |
| Quick run command | `just test-bucket review` (single bucket, needs `just test-db` + env) or `uv run pytest tests/review -q` |
| Full suite command | `just integration-test` (self-contained ephemeral PG+Redis, auto-teardown) |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| COV-01 | `services/review.py` ≥ 85% via behavior-asserting tests | unit (service) | `uv run pytest tests/review/services/test_review_degrade.py -q` | ❌ Wave 0 (new) |
| COV-01 | `agent_liveness.classify_compute_lanes` degrade path covered (margin) | unit (service) | `uv run pytest tests/agents/services/test_agent_liveness.py -q` | ✅ (extend existing) |
| COV-02 | Per-module floor script fails on a sub-floor module | unit (script) | `uv run pytest tests/shared/**/test_coverage_floor.py -q` | ❌ Wave 0 (new — test the script itself) |
| COV-02 | Global `fail_under` raised & the two edit sites agree | guard | `uv run pytest tests/shared/**/test_coverage_gate.py -q` | ❌ Wave 0 (new — mirror Phase 63 guard-test style) |
| COV-01/02 | Combined gate + floor both green end-to-end | integration | `just coverage-combine` (after a full combined run) | ✅ (recipe exists; extend) |

### Sampling Rate
- **Per task commit:** the touched bucket, e.g. `just test-bucket review` (isolation-safe — MEMORY: bucket isolation).
- **Per wave merge:** `just integration-test` (full suite, ephemeral services).
- **Phase gate:** full combined run green + `just coverage-combine` (global gate + per-module floor) green before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `scripts/coverage_floor.py` — the per-module floor check (COV-01/D-02).
- [ ] `tests/shared/core/test_coverage_floor.py` — unit-test the floor script (feed a synthetic `coverage.json` with a sub-floor module → assert exit 1; all-pass → exit 0; exemption honored). The script is CI-load-bearing, so it needs its own tests (Phase 63 precedent: `scripts/classify-changed-files.sh` is tested by `tests/shared/test_change_gate.py`).
- [ ] `tests/shared/core/test_coverage_gate.py` — guard asserting `pyproject.toml fail_under` == the `justfile coverage-combine --fail-under` number, and both > 90.38.
- [ ] `tests/review/services/test_review_degrade.py` — the `review.py` behavior-asserting uplift.
- [ ] (optional margin) extend `tests/agents/services/test_agent_liveness.py` for the `classify_compute_lanes` `SQLAlchemyError` branch.

## Security Domain

> `security_enforcement` not explicitly disabled; included for completeness.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V5 Input Validation | minimal | The floor script reads a locally-generated `coverage.json` (trusted CI artifact), not external input — no injection surface |
| V14 Config/Build | yes | CI gate integrity: the required check must fail-closed. `just coverage-combine` exits non-zero on gate/floor failure → red required check (fail-closed verified) |

### Known Threat Patterns
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Coverage gate silently passing (fail-open) on a tooling error | Repudiation / Tampering | Script returns explicit exit codes; missing/empty `coverage.json` should raise (not pass). Test the failure path. |
| `bandit`/`ruff S` on the new script | — | Script is pure stdlib; `subprocess`/`eval`-free. `T20` print calls need `# noqa: T201` or place under a `scripts/**` ruff per-file-ignore (note: current ignore is `scripts/parity/**` only — add `scripts/coverage_floor.py` to `T201` allowances or keep the `# noqa`). |

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | `diff-cover` / `coverage-conditional-plugin` / `pytest-cov` have no per-file-floor feature | Mechanism | LOW — if one did, it'd still add a dependency the project avoids; custom script remains preferred |
| A2 | Single-process full-suite coverage (96.89%) equals the CI per-bucket `coverage combine` result | Re-Baselining | LOW — the number matches Phase 63's reported combined 96.89% exactly; planner re-runs CI-faithful to pin the final gate digit anyway |
| A3 | The `combine` job is the branch-protection required check, so a non-zero `coverage-combine` blocks merge | Gate Wiring | MEDIUM — MEMORY notes branch-protection required-check = `aggregate-results` was a deferred post-Phase-63 chore (UAT #11); confirm the required check actually gates on the combine result before relying on red-check enforcement |
| A4 | `.planning/config.json` does not disable nyquist_validation / security_enforcement | Validation/Security | LOW — sections included regardless; planner drops if config says otherwise |

## Open Questions (RESOLVED)

1. **Is the branch-protection required check wired to the combine result yet?** _Resolved → 64-04:_ plan 64-04 adds a `gh api .../required_status_checks` read (Task 1) + a blocking human-verify checkpoint (Task 2) that confirms/sets the combine (or `aggregate-results`) context as a merge-blocking required check, or explicitly defers it as a tracked chore.
   - What we know: Phase 63 intended the required check to be a stable aggregate/combine job; MEMORY flags "set GitHub branch-protection required-check = `aggregate-results`" as a deferred post-merge chore (Phase 63 UAT #11).
   - What's unclear: whether a red `combine` job currently blocks merge in branch protection.
   - Recommendation: the planner should include a verification step (or a note) confirming the required check gates on the combine job; otherwise the floor/gate is advisory in practice. This does not block writing the script/tests.

2. **Exact final `<NEW_GLOBAL>` digit.** _Resolved → 64-03:_ plan 64-03 Task 1 measures the post-uplift combined overall the CI-faithful way at execute time and pins `fail_under` = integer floor of (overall − ~1), strictly > 90.38.
   - What we know: combined overall = 96.89% today; post-uplift ≥ that; D-05 = achieved-minus-~1.
   - What's unclear: the precise post-uplift overall (depends on how much margin the planner adds).
   - Recommendation: measure at execute time (CI-faithful commands above), then set `fail_under` to the integer floor of (overall − 1), e.g. 95. Strictly > 90.38 guaranteed.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Docker (colima) | `just test-db` ephemeral PG+Redis for coverage measurement | ✓ | postgres:18-alpine / redis:7-alpine pulled & run this session | CI service containers (tests.yml) |
| uv | all commands | ✓ | project standard | — |
| coverage.py | combine/json/report | ✓ | 7.14.2 | — |
| just | recipe runner | ✓ | present | — |

**No missing dependencies.** Coverage was measured end-to-end this session.

## Sources

### Primary (HIGH confidence)
- Local coverage run (2566 tests, live PG:5433 + Redis:6380, 2026-07-02): combined 96.89%; sub-floor set `{services/review.py 83.16%}`; no-DB run 68.16% reproducing the stale offender numbers. `[VERIFIED]`
- `coveragepy/coveragepy` (Context7): `coverage json` schema (`files[*].summary.percent_covered`), `report/json --fail-under` is total-only, `--format=total`. `[CITED]`
- Repo files read: `pyproject.toml`, `justfile`, `.github/workflows/tests.yml`, `tests/conftest.py`, `tests/buckets.json`, `src/phaze/services/{review,agent_liveness,pipeline}.py`, `src/phaze/routers/shell.py`, `src/phaze/main.py`, `tests/shared/core/test_shell_routes.py`, `tests/shared/services/test_pipeline.py`. `[VERIFIED]`
- Tool versions: `coverage 7.14.2`, `pytest 9.1.1`, `pytest-cov 7.1.0`, `pytest-asyncio 1.4.0`. `[VERIFIED: uv/import]`

### Secondary (MEDIUM confidence)
- `.planning/phases/63-*/63-CONTEXT.md` (D-02 combine, D-05 bucket dirs, D-06 partition guard, D-10 just-delegation); `.planning/STATE.md` (Phase 63 shipped, bucket isolation, 96.89% reorg number).

### Tertiary (LOW confidence)
- Absence of a per-file-floor feature in `diff-cover`/`coverage-conditional-plugin`/`pytest-cov` — reasoned from tool purpose, not exhaustively tool-tested (A1).

## Metadata

**Confidence breakdown:**
- Authoritative coverage map / re-baselining: HIGH — measured directly this session, matches Phase 63's combined number.
- Floor mechanism (custom `coverage json` script): HIGH — coverage.py JSON schema + total-only `fail_under` verified against current docs.
- Gate wiring edit sites: HIGH — read the exact files/lines.
- Branch-protection enforcement of the red check: MEDIUM — deferred Phase 63 chore; flagged (A3/Open-Q1).

**Research date:** 2026-07-02
**Valid until:** ~2026-08-01 (coverage numbers drift as code lands; re-measure at execute time to pin the final gate digit).
