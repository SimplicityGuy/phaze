---
phase: 89-legacy-scan-path-deletion-sentinel-reattribution
plan: 02
subsystem: database / migration
tags: [legacy-retirement, data-migration, agent-attribution, restrict-fk, single-transaction]
requires:
  - "agents.kind marker (migration 024) + revoked_at (migration 012) for the fileserver auto-detect predicate"
  - "The 012-seeded legacy sentinel agent + its status='live' watcher batch + RESTRICT FKs on files/scan_batches"
  - "LEGACY-03 model half (Plan 89-01): agent_id has no Python default, so no new row can be silently attributed to the sentinel"
provides:
  - "Migration 038: reattributes all legacy-application-server-owned files + non-live scan_batches to the auto-detected sole fileserver, then deletes the sentinel Agent row (LEGACY-02, LEGACY-03 migration half)"
  - "An -x reattribute_to=<id> override (validated, parameterized) for the >1-fileserver case"
  - "A COUNT=0 pre-DELETE assertion inside one transaction (D-09) guaranteeing the RESTRICT FK is satisfiable"
affects:
  - "Prod ship-time: alembic upgrade head reattributes ~11,428 legacy-owned rows to nox and removes the sentinel"
  - "Any future head-migration test that inserts agent_id='legacy-application-server' post-038 will hit an FK violation (the sentinel is gone) -- must self-seed a real fileserver"
tech-stack:
  added: []
  patterns:
    - "Raw parameterized sa.text data migration (migration-012 style), NO model imports, NO DDL"
    - "context.get_x_argument(as_dictionary=True).get('reattribute_to') operator override, validated against agents before use"
    - "Ordered single-txn: DELETE legacy live watcher batch -> UPDATE files -> UPDATE scan_batches -> COUNT=0 assert -> DELETE sentinel"
    - "Migration test teardown via _reset_schema (DROP/CREATE public) instead of downgrade_to('base') -- required because 038.downgrade() raises NotImplementedError"
key-files:
  created:
    - alembic/versions/038_retire_legacy_sentinel.py
    - tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py
  modified: []
  deleted: []
decisions:
  - "D-03 refined (Pitfall 1): the legacy status='live' watcher batch is DELETED, not reattributed -- reattributing it would create a second live row for the target and violate uq_scan_batches_agent_id_live"
  - "A2 resolved: cfg.cmd_opts = argparse.Namespace(x=['reattribute_to=<id>']) DOES surface the override to context.get_x_argument -- no env.py-level fallback needed"
  - "D-10: downgrade() raises NotImplementedError (irreversible); tests tear down via _reset_schema so teardown never trips the raise"
  - "The migration file is NOT mypy-excluded (only tests/prototype/services are) -- upgrade/downgrade fully typed, mypy strict clean"
metrics:
  tasks: 2
  commits: 2
  files_changed: 2
  tests_passing: 11
  duration: ~50m
  completed: 2026-07-11
---

# Phase 89 Plan 02: Legacy Sentinel Reattribution Migration Summary

Wrote Alembic migration `038` that reattributes every historical `legacy-application-server`-owned `files` row and non-live `scan_batches` row to the auto-detected sole `kind='fileserver'` agent (with an `-x reattribute_to=<id>` override for ambiguity), DELETEs the vestigial migration-012 `status='live'` watcher batch to dodge the `uq_scan_batches_agent_id_live` collision, gates the sentinel delete behind a COUNT=0 assertion inside one transaction, and raises `NotImplementedError` on downgrade — delivering LEGACY-02 and the migration half of LEGACY-03, proven by an 11-test integration suite covering all 8 planned scenarios.

## What shipped

- **LEGACY-02 (reattribution + backfill-verify):** `alembic/versions/038_retire_legacy_sentinel.py` (`revision="038"`, `down_revision="037"`). Target selection auto-detects the sole non-revoked fileserver via `revoked_at IS NULL AND kind='fileserver'` (the legacy agent is auto-excluded — 012 seeds it `revoked_at=NOW()`); 0 → abort, >1 → abort with `pass -x reattribute_to=<id>` guidance, exactly 1 → use it. The `-x` override is validated against the same predicate and passed via `bindparams(id=...)` — never f-stringed (T-89-02-01). Ordered single-transaction body: (1) DELETE the legacy live watcher batch, (2) `UPDATE files SET agent_id=:target`, (3) `UPDATE scan_batches SET agent_id=:target`, (4) `COUNT(*)` legacy-owned across files ∪ scan_batches must be 0 else `raise` (rolls back before the DELETE), (5) `DELETE FROM agents WHERE id='legacy-application-server'`.
- **LEGACY-03 (migration half — sentinel delete):** the sentinel `Agent` row is deleted after reattribution, RESTRICT-FK-ordered inside the same transaction; `downgrade()` raises `NotImplementedError` documenting that original ownership is unrecoverable (D-10), deviating from the no-op `pass` downgrades in 035/036.
- **No DDL (D-07):** the migration adds zero schema — 012 added `agent_id` nullable + backfilled, so there is no `server_default` to drop. Scenario 8 asserts an empty autogenerate diff, and `alembic heads` reports a single head `038`.
- **Test suite (8 scenarios, 11 tests):** `tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py` — reattribution+sentinel-delete, Pitfall-1 live-batch no-collision, abort-on-0-fileserver (rollback proof), abort-on->1 (message asserts `pass -x reattribute_to`), `-x` override select + invalid-target reject, `NotImplementedError` downgrade, empty autogenerate diff, plus the static bare-number + `saq_jobs` grep guard + no-f-string-target assertions. Green against real PG (:5433) in ~7s.

## Deviations from Plan

**1. [Rule 3 - Blocking] Scenario 3 relies on the migration-012 legacy live batch instead of seeding a second one**
- **Found during:** Task 2 first test run — seeding a second `status='live'` batch for `legacy-application-server` raised `UniqueViolationError` on `uq_scan_batches_agent_id_live` *in the seed itself*.
- **Issue:** The plan's literal scenario-3 wording ("seed BOTH a legacy `status='live'` batch AND a target-agent live batch") is infeasible: migration 012 already seeds exactly one legacy live watcher batch at rev 037, so a second one collides before 038 ever runs.
- **Fix:** Scenario 3 now seeds only the *target's* own live batch, asserts the pre-existing legacy live batch count is 1 (from 012), runs 038, and asserts it drops to 0 while nox keeps its single live batch. This is strictly more realistic — it exercises the actual production sentinel, not a synthetic duplicate. Intent (prove the legacy live batch is DELETED, not reattributed, with no `uq_scan_batches_agent_id_live` collision) is fully preserved.
- **Commit:** a4bbec8f

## A2 harness resolution (documented per plan Task-2 action)

The plan flagged A2 (no in-repo precedent for programmatic `-x`) and asked to document the chosen mechanism. **Resolved: `cfg.cmd_opts = argparse.Namespace(x=["reattribute_to=<id>"])` set on the Alembic `Config` before `upgrade_to` DOES surface the override** — `context.get_x_argument(as_dictionary=True)` reads `config.cmd_opts.x`, and `command.upgrade(cfg, rev)` preserves the cfg's `cmd_opts`. Scenarios 6 and the invalid-target reject both drive the override this way; no `env.py`-level injection fallback was needed.

## Verification

- `uv run pytest tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py` → **11 passed** (with `MIGRATIONS_TEST_DATABASE_URL`/`TEST_DATABASE_URL` pointed at :5433 per the port footgun).
- Task-1 automated verify passes: module imports, `revision=='038'`/`down_revision=='037'`, `downgrade()` raises `NotImplementedError`.
- All Task-1 acceptance greps pass: `revision="038"`, `down_revision="037"`, the `revoked_at IS NULL AND kind='fileserver'` auto-detect predicate, `status='live'` live-batch DELETE, `NotImplementedError`, and the `saq_jobs` guard returns 0 (no SQL touches saq_jobs). No f-string interpolation of the target id — asserted by `test_target_id_is_never_f_string_interpolated`.
- `ruff check` + `ruff format --check` clean on both files; `mypy .` clean (the migration is NOT mypy-excluded and passes strict); pre-commit hooks (ruff/ruff-format/bandit/mypy) green on both commits.
- `uv run alembic heads` → single head `038` (chains cleanly off 037).

## Notes for downstream

- **Ship-time (out of this plan):** rehearse `alembic upgrade head` against a restore of the prod corpus — confirm 0 legacy-owned rows remain and the sentinel is deleted. Prod has one real fileserver (nox), so the auto-detect path resolves with no `-x` needed.
- **Head-migration test caution (RESEARCH):** any test using the `migrated_engine` fixture (upgrades to head, now including 038) that inserts `agent_id='legacy-application-server'` without seeding that agent will now hit an FK violation — the sentinel is deleted post-038. New head-level tests must self-seed a real fileserver. Historical migration tests pinned to revisions ≤ 037 are unaffected.
- Phase 90 (destructive `034`/FileState work) is untouched here — 038 adds no DDL and never writes `files.state`.

## Self-Check: PASSED

- `alembic/versions/038_retire_legacy_sentinel.py` — FOUND
- `tests/integration/test_migrations/test_migration_038_retire_legacy_sentinel.py` — FOUND
- Commit `16d304dc` (feat migration) — FOUND
- Commit `a4bbec8f` (test) — FOUND
