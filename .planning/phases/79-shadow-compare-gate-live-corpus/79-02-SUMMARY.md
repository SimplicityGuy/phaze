---
phase: 79-shadow-compare-gate-live-corpus
plan: 02
subsystem: testing
tags: [shadow-compare, cli, argparse, justfile, migration-gate, live-corpus, exit-code]

# Dependency graph
requires:
  - phase: 79-shadow-compare-gate-live-corpus
    plan: 01
    provides: "run_shadow_compare / Report shared assertion core (D-01) this CLI wraps without duplicating logic"
provides:
  - "src/phaze/cli/shadow_compare.py — thin `python -m phaze.cli.shadow_compare` argparse runner over the shared core (entry point B, D-01); exit 1 iff hard divergence (D-05); --database-url live-restore override (D-02)"
  - "justfile [group('db')] shadow-compare *ARGS recipe — the operator/rollout entry SC-3 records in VERIFICATION"
  - "tests/integration/test_shadow_compare.py -k cli — locks the D-05 exit-code contract (1 on divergent, 0 on clean) driven through the CLI"
affects: [90-destructive-033-migration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Dual-entry over ONE core: the CLI imports run_shadow_compare from phaze.services.shadow_compare — no second copy of the assertion logic (D-01)"
    - "make_url(dsn).host/.database surfaces at most host/db for a --database-url override; the full DSN never reaches stdout or a logger (T-79-04)"
    - "CLI-exit test cells are SYNC (main() owns its asyncio.run) and drive --database-url so main builds its own engine in its own loop; committed corpus + TRUNCATE agents CASCADE teardown keeps the rollback-isolated cells hermetic"

key-files:
  created:
    - src/phaze/cli/shadow_compare.py
  modified:
    - justfile
    - tests/integration/test_shadow_compare.py

key-decisions:
  - "CLI-exit cells drive the --database-url path (not the default-async_session path) so main() builds its OWN engine inside its OWN event loop — avoids the `asyncio.run() cannot be called from a running event loop` failure and cross-loop asyncpg engine sharing, while also exercising the D-02 live-restore override"
  - "Teardown TRUNCATEs `agents CASCADE` (not just `files`): a CLI file defaults agent_id to the RESTRICT-FK legacy agent, so the committed corpus needs the agent committed too — leaving it behind collides with every sibling test's per-test agent seed (pk_agents UniqueViolation)"

patterns-established:
  - "Operator/rollout gate entry: `just shadow-compare --database-url <restore-dsn>` returns nonzero on hard divergence — the recorded path for the DEFERRED live 200K run (D-02)"

requirements-completed: [MIG-02]

# Metrics
duration: ~30min
completed: 2026-07-08
---

# Phase 79 Plan 02: Shadow-Compare CLI + db-group Recipe Summary

**A thin `python -m phaze.cli.shadow_compare` argparse runner and a `[group('db')] shadow-compare *ARGS` justfile recipe that drive the SAME Plan-01 `run_shadow_compare` core (D-01) — exit 1 iff any HARD invariant diverges / 0 on a clean corpus (D-05), with a `--database-url` live-restore override (D-02) whose DSN is never leaked, locked by a sync CLI-exit test cell.**

## Performance

- **Duration:** ~30 min
- **Tasks:** 2
- **Files created:** 1 (+2 modified)

## Accomplishments
- `src/phaze/cli/shadow_compare.py` (105 lines): `configure_logging()` first, `_build_parser()` exposing `--sample-cap` (`type=int`), `--verbose` (`store_true`, uncaps the sample), `--database-url` (default None); `main()` returns `1 if report.hard_fail_total else 0` (D-05); imports `run_shadow_compare` from `phaze.services.shadow_compare` and defines NO invariant/comparison logic of its own (D-01)
- `_run()` uses the default `async_session` when `--database-url` is None; otherwise builds a fresh async engine from the DSN (a live-corpus restore, D-02) and disposes it after the run
- Information-disclosure mitigation (T-79-04): the raw `--database-url`/DSN is never passed to `print()` or a logger; `_safe_target()` uses `sqlalchemy.engine.make_url` to surface at most `host/db`
- `justfile` `[group('db')] shadow-compare *ARGS:` runs `uv run python -m phaze.cli.shadow_compare {{ ARGS }}` — the variadic threads `--verbose`/`--sample-cap`/`--database-url` through; `just --evaluate` parses clean; `just shadow-compare --help` lists all three flags
- Two sync CLI-exit cells appended to `tests/integration/test_shadow_compare.py`: `main(["--database-url", SA_DSN])` returns 1 on a seeded-divergent corpus and 0 on a clean one (D-05); `just test-bucket integration` green IN ISOLATION (130 passed, up from Plan-01's 128)

## Task Commits

Each task was committed atomically:

1. **Task 1: Thin CLI runner (src/phaze/cli/shadow_compare.py)** - `9949c4c7` (feat)
2. **Task 2: db-group recipe + CLI-exit test cell** - `713da671` (test)

## Files Created/Modified
- `src/phaze/cli/shadow_compare.py` - thin argparse runner over the shared core (entry point B, D-01); `--sample-cap`/`--verbose`/`--database-url`; exit-code contract (D-05); DSN-safe output (T-79-04)
- `justfile` - `[group('db')] shadow-compare *ARGS` recipe
- `tests/integration/test_shadow_compare.py` - two sync `-k cli` cells locking the D-05 exit-code contract via the `--database-url` path, with committed-corpus + `TRUNCATE agents CASCADE` hermetic teardown

## Decisions Made
- **CLI cells drive `--database-url`, not the default sessionmaker:** `main()` owns its own `asyncio.run`, so calling it from inside pytest-asyncio's running loop RuntimeErrors. Making the cells sync and passing `--database-url SA_DSN` lets `main()` build its own engine in its own loop (no cross-loop asyncpg sharing) and doubles as coverage of the D-02 live-restore path.
- **Teardown truncates `agents CASCADE`:** a committed CLI file defaults `agent_id` to the RESTRICT-FK `legacy-application-server` agent, which must therefore also be committed; leaving it behind collided with every sibling test's per-test agent seed (`pk_agents` UniqueViolation). Truncating the agent (cascading to files) restores a clean table for the rollback-isolated cells.

## Deviations from Plan

None - plan executed exactly as written. (The choice of the `--database-url` path for the test cells is an implementation detail the plan left open — "call `main([])` or with `--sample-cap`" — and satisfies the same D-05 acceptance criteria.)

## Issues Encountered
- **First test draft used the default-`async_session` path:** `main()`'s internal `asyncio.run` cannot run inside pytest-asyncio's loop → `RuntimeError: asyncio.run() cannot be called from a running event loop`. Fixed by making the cells sync + using `--database-url`.
- **Committed-agent contamination:** the initial teardown truncated only `files`, leaving the committed FK agent to break `test_stage_status_equivalence.py` (60 errors, `pk_agents` UniqueViolation). Fixed by truncating `agents CASCADE`; the already-committed contamination was cleared from the shared DB. Full bucket then green (130 passed).

## Next Phase Readiness
- The gate now has both entry points over the single core: the CI test cell (Plan 01) and the operator/rollout CLI (this plan). `just shadow-compare --database-url <restore-dsn>` is the recorded path for the DEFERRED live 200K corpus run (D-02, homelab). Phase 90's destructive `033` remains gated on `hard_fail_total == 0` on that live corpus.
- No blockers.

## Self-Check: PASSED

- FOUND: src/phaze/cli/shadow_compare.py
- FOUND: justfile shadow-compare recipe (`[group('db')]`)
- FOUND: tests/integration/test_shadow_compare.py CLI-exit cells (`test_cli_main_exits_*`)
- FOUND commits: 9949c4c7 (Task 1), 713da671 (Task 2)

---
*Phase: 79-shadow-compare-gate-live-corpus*
*Completed: 2026-07-08*
