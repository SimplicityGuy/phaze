---
phase: 84-dedup-fingerprint-progress-cutover
plan: 05
subsystem: testing / anti-drift guards
tags: [ast-guard, source-scan, dedup, fingerprint, read-04, d-14, mutation-tested]
requires:
  - "84-03: dedup.py cutover (nine reads → ~dedup_resolved_clause(), one surviving D-00a writer)"
  - "84-04: fingerprint.py cutover (get_fingerprint_progress derived, zero FileState.FINGERPRINTED)"
provides:
  - "DB-free AST source-scan guard forbidding a FileRecord.state read regression in dedup.py + fingerprint.py"
affects:
  - "tests/shared/ (new guard, shared bucket)"
tech-stack:
  added: []
  patterns:
    - "ast.parse + ast.walk source-assertion guard (model: tests/analyze/services/test_single_awaiting_writer.py)"
    - "walks Call.args AND Call.keywords (positional + keyword read detection); classifier fed source STRINGS so mutation directions are encoded hermetically"
key-files:
  created:
    - "tests/shared/test_dedup_fingerprint_source_scan.py"
  modified: []
decisions:
  - "Guard is AST-based (not grep): the DUPLICATE_RESOLVED chain would false-positive a grep on the surviving writer, and FINGERPRINTED lives in a fingerprint.py docstring a line scan would flag."
  - "Read = inside a Compare OR any arg (positional or keyword) of where/filter/filter_by/having; a read context wins over write classification (conservative)."
  - "Reused the single classifier over crafted source strings for the five mutation directions, so teeth are proven hermetically in-repo AND leave no source dirty (belt-and-suspenders alongside the live-source mutation run recorded below)."
metrics:
  duration: "~15 min"
  completed: "2026-07-09"
  tasks: 1
  files: 1
---

# Phase 84 Plan 05: AST Source-Scan Guard (D-14) Summary

A DB-free `ast.walk` guard that permanently forbids reintroducing a `FileState.DUPLICATE_RESOLVED` read in `services/dedup.py` (while tolerating the one surviving D-00a dual-writer) and any `FileState.FINGERPRINTED` in `services/fingerprint.py` — the standing insurance behind READ-04 for these two files.

## What Was Built

`tests/shared/test_dedup_fingerprint_source_scan.py` (shared bucket, no PG / no settings / no saq_jobs). It reads each service module from disk, `ast.parse`s it, and classifies every `FileState.<member>` attribute node:

- **WRITE** — RHS (nested) of an `Assign` whose target ends in `.state` (the surviving `f.state = FileState.DUPLICATE_RESOLVED` dual-writer, D-00a, retired Phase 90).
- **READ-IN-COMPARE** — inside an `ast.Compare`.
- **READ-IN-WHERE** — inside ANY argument of a `where`/`filter`/`filter_by`/`having` `Call`. The walker iterates the positional `Call.args` list AND the `Call.keywords` list, keying on neither `keyword.arg` nor a chained comparator — closing both Phase-83 blind spots (positional `.where(a, b, c)` and `**splat`/keyword args).

Assertions: `dedup.py` has exactly one WRITE, zero reads, zero other occurrences (total == 1); `fingerprint.py` has zero `FileState.FINGERPRINTED` attribute accesses (its docstring `FINGERPRINTED` prose is invisible to an AST attribute scan).

The classifier takes source **strings**, so the five mutation directions are also encoded as permanent hermetic negative tests that never touch the real files.

## Mutation Verification (live source, observed verbatim)

Each mutation was applied to the REAL source file via `uv run python`, the guard run, then restored with `git checkout --`. `git status --short src/` was clean afterward (SRC CLEAN).

| # | Mutation | Expected | Observed |
|---|----------|----------|----------|
| 1 | Reintroduce a read as a **positional** 2nd arg in `resolve_group`'s `.where(...)` in `dedup.py` | RED | `1 failed` ✅ |
| 2 | Reintroduce a read as a **keyword** arg `.filter_by(state=FileState.DUPLICATE_RESOLVED)` in `dedup.py` | RED | `1 failed` ✅ |
| 3 | Surviving dual-writer at `dedup.py` left untouched (false-positive check) | GREEN | `1 passed` ✅ |
| 4 | Reintroduce `FileState.FINGERPRINTED` in `fingerprint.py` | RED | `1 failed` ✅ |
| 5 | `FINGERPRINTED` docstring prose at `fingerprint.py` (false-positive check) | GREEN | `1 passed` ✅ |

(A first mutation attempt used a bare `python` heredoc that hit the `mise` no-shim error and silently applied nothing — the "pass" that produced was a non-mutation. Re-run with `uv run python` applied each mutation and produced the RED/GREEN table above. `git status --short src/` confirmed clean after every restore.)

The keyword `.filter_by` case (mutation #2) is a genuine coverage add over Compare-detection alone: a keyword read is not wrapped in a Compare, so only the `Call.keywords` walk catches it.

## Verification

- `uv run pytest tests/shared/test_dedup_fingerprint_source_scan.py -q` → **8 passed**.
- Same file under the exact `test-bucket` recipe flags (`--cov=phaze --cov-report= --cov-fail-under=0`) → **8 passed** in isolation.
- `uv run ruff check` → **All checks passed**; `ruff format` applied.
- DB-free: imports only `ast` + `pathlib`; constructs no `Settings`, imports no `phaze.database`/`phaze.models`/`saq`.

## Deviations from Plan

None — plan executed as written.

### Notes (not deviations)

- The full `just test-bucket shared` run reported pre-existing bucket-wide flakes (`3 failed, 678 passed, 325 errors`) from the known shared-bucket isolation hazards (`get_settings` lru_cache leak / cross-test setup errors). **Zero** of those failures/errors are this file — the new guard passes standalone and under bucket flags. Success criterion ("passes via `just test-bucket shared` in isolation") is met; the bucket-wide flakiness is out of scope (pre-existing, unrelated to a DB-free additive test).

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: tests/shared/test_dedup_fingerprint_source_scan.py
- Commit hash recorded below.
