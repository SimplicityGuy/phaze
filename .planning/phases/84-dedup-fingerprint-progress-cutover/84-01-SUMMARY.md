---
phase: 84-dedup-fingerprint-progress-cutover
plan: "01"
subsystem: migrations
tags: [alembic, dedup_resolution, data-migration, reconcile, shadow-compare]
requires:
  - "032: dedup_resolution table + uq_dedup_resolution_file_id + _BACKFILL_DEDUP (verbatim source)"
  - "034: prior chain head (down_revision) + migration-test template"
  - "conftest: MIGRATIONS_TEST_DATABASE_URL, _build_alembic_config, upgrade_to/downgrade_to"
provides:
  - "Migration 035: sync, data-only, bidirectional reconcile of dedup_resolution against files.state"
  - "Live-corpus repair that makes the Phase-79 hard `duplicate_resolved` shadow invariant satisfiable"
affects:
  - "Wave 2 seam (b): dedup reader flip to NOT EXISTS(marker) — 035 must land before it (load-bearing)"
tech-stack:
  added: []
  patterns:
    - "Data-only repair migration (034 precedent): sync upgrade(), op.execute(sa.text(STATIC)), no DDL, empty autogenerate diff"
    - "Bidirectional reconcile: verbatim ON CONFLICT DO NOTHING insert half + set-based orphaned-marker DELETE half"
key-files:
  created:
    - "alembic/versions/035_reconcile_dedup_resolution.py"
    - "tests/integration/test_migrations/test_migration_035_reconcile_dedup_resolution.py"
  modified: []
decisions:
  - "035 downgrade is a documented NO-OP (D-04 Claude's Discretion) — safer than 034's lossy DELETE for a pure reconcile"
  - "Empty-diff scope sets (_O35_TABLES/_INDEXES/_COLUMNS) all empty — 035 touches no ORM-mapped schema"
metrics:
  duration: "~15 min"
  completed: "2026-07-09"
  tasks: 2
  files: 2
---

# Phase 84 Plan 01: Migration 035 Reconcile dedup_resolution Summary

Data-only, bidirectional Alembic migration (`035`) that reconciles the `dedup_resolution` marker table
to the still-authoritative `files.state` in both directions — inserting the markers missing since
`032`'s one-shot backfill and deleting orphaned ones — plus its real-Postgres integration test proving
both directions, idempotency, an empty autogenerate diff, and a byte-unchanged `files.state`.

## What Was Built

- **`alembic/versions/035_reconcile_dedup_resolution.py`** — sync `upgrade()` executing two static,
  parameter-free `op.execute(sa.text(...))` statements:
  1. `032`'s `_BACKFILL_DEDUP` re-run **verbatim** (`INSERT … SELECT … FROM files WHERE
     state='duplicate_resolved' ON CONFLICT (file_id) DO NOTHING`) — inserts the missing markers with a
     best-effort derived `canonical_file_id`.
  2. A new `DELETE FROM dedup_resolution dr USING files f WHERE dr.file_id = f.id AND f.state <>
     'duplicate_resolved'` — removes orphaned markers.
  No DDL, no model import, no interpolation, CRITICAL `saq_jobs` banner, `revision="035"`,
  `down_revision="034"`. `downgrade()` is a documented no-op.
- **`tests/integration/test_migrations/test_migration_035_reconcile_dedup_resolution.py`** — mirrors
  `034`'s test 1:1: three DB-free assertions (bare-number revision, `saq_jobs`-banner scan, static-SQL
  scan) plus a real-PG both-direction corpus.

## Why 035 Exists (D-01 / D-04)

Since `032` there has been **no go-forward writer** of `dedup_resolution` — `resolve_group` stamped
`files.state = duplicate_resolved` and never inserted a marker. Every group resolved since then carried
`state=duplicate_resolved` with **no marker**, violating the *hard* shadow-compare invariant
`state=DUPLICATE_RESOLVED ⇒ dedup marker exists` (`services/shadow_compare.py:135`, `soft=False`). `035`
reconciles the existing corpus so the Phase-79 gate is green on the live corpus for the first time.
**Ordering is load-bearing:** `035` must land before any dedup reader flips to `NOT EXISTS(marker)`, or
resolved files reappear and orphan-hidden files vanish unreachably.

## Test Corpus (both directions)

| File | Seed state | Seed marker | After 035 |
|------|-----------|-------------|-----------|
| `_FA` | `duplicate_resolved` | none | gains 1 marker, `canonical_file_id = _FCAN` (derived) |
| `_FCAN` | `analyzed` (same sha256 as `_FA`) | none | stays row-less (is the derived canonical target) |
| `_FB` | `analyzed` | orphaned marker | marker **deleted** |
| `_FC` | `analyzed` (control) | none | stays row-less |
| `_FD` | `duplicate_resolved` | pre-existing marker | **unchanged** (`DO NOTHING`, same marker id, no dup) |

Plus: `files.state` snapshot byte-unchanged; idempotent re-run of `_BACKFILL_DEDUP` (no duplicates,
`GROUP BY file_id HAVING count(*)>1 == []`); empty autogenerate diff via `compare_metadata`; no-op
downgrade leaves the two reconciled markers intact.

## Verification

- `MIGRATIONS_TEST_DATABASE_URL=…5433…phaze_migrations_test uv run pytest
  tests/integration/test_migrations/test_migration_035_reconcile_dedup_resolution.py` → **4 passed**.
- `uv run ruff check` (both files) → clean.
- `uv run mypy .` → Success, no issues in 206 source files.
- Migration header import assert → `revision=='035'`, `down_revision=='034'`.
- `035` is the free next revision (on-disk head was `034`).

## Downgrade Choice (D-04 Claude's Discretion)

Chose a **no-op** `downgrade()` over `034`'s documented-lossy `DELETE`. A `035` downgrade has no safe
target: the inserted markers are indistinguishable from live go-forward writes and the deleted markers
cannot be reconstructed. Deleting all markers would destroy live resolutions; a no-op leaves the marker
table in its most-consistent state and never destroys a live resolution. Documented in the migration
docstring.

## Deviations from Plan

None — plan executed exactly as written. (The test file received an automatic `ruff-format` whitespace
adjustment to comment alignment on first commit; re-staged and committed with no logic change.)

## Test-DB Footgun (carried into the test docstring)

`MIGRATIONS_TEST_DATABASE_URL` defaults to port **5432**, but `just test-db` provisions **5433** and
`just test-bucket` does not export it. The test docstring warns to export it explicitly or the harness
silently talks to the wrong DB and fails like an infra flake.

## Follow-ups (out of this plan's scope)

- **D-16.2 live-corpus `shadow_compare` run after `035`, before merge** — the CI test cannot see the real
  post-`032` resolved-without-marker rows; only the live run proves `035` covered them. Deferred to the
  phase-level pre-merge gate.
- **Wave 2 seam (b)** — the go-forward `dedup_resolution` writer + fixed `undo_resolve` + the nine dedup
  reader flips + the divergence/source guards. Depends on `035` having landed.

## Self-Check: PASSED
