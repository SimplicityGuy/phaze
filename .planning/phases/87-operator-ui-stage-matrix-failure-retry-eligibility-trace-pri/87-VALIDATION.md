---
phase: 87
slug: operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-11
---

# Phase 87 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Critical behaviors derived from 87-RESEARCH.md "## Validation Architecture".

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (via `uv run pytest`); per-bucket isolation (`just test-bucket <bucket>`) |
| **Config file** | pyproject.toml (`[tool.pytest.ini_options]`); buckets in tests/buckets.json |
| **Quick run command** | `uv run pytest tests/<bucket> -q` (bucket of the touched file) |
| **Full suite command** | `uv run pytest -q` (or `just test`) |
| **Estimated runtime** | ~per-bucket seconds; full suite minutes |

> DB-touching tests require `TEST_DATABASE_URL` + `PHAZE_QUEUE_URL` pointed at the `:5433` ephemeral DB (conftest defaults to `:5432`). Migration tests also need `MIGRATIONS_TEST_DATABASE_URL` (port footgun: `just test-db` provisions 5433).

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/<bucket> -q` for the touched bucket (must pass in isolation via `just test-bucket <bucket>`).
- **After every plan wave:** Run the full suite.
- **Before `/gsd:verify-work`:** Full suite green + 90% coverage floor + `uv run mypy .` + `uv run ruff check .`.
- **Max feedback latency:** per-bucket run.

---

## Per-Task Verification Map

> Filled by the planner per task. The critical behaviors below MUST each map to at least one automated test.

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 87-01-01 | 01 | 1 | UI-04 | — | skip marker write records reason + is additive-only | integration (migration) | `uv run pytest tests/integration/test_migrations/ -q` | ❌ W0 | ⬜ pending |

---

## Critical Behaviors (Nyquist sample points — MUST be automated)

Derived from RESEARCH §Validation Architecture. Each is a correctness-load-bearing behavior of the `skipped`-marker slice or the paginated surface:

1. **skipped-file-leaves-pending-set** — a file with a `skipped` marker for stage S is ABSENT from `get_metadata_pending_files` / `get_fingerprint_pending_files` / `get_discovered_files_with_duration` for S (via `~skipped` in `eligible_clause`). One test per enrich stage.
2. **skipped-reads-as-distinct-bucket** — `stage_status_case(S)` returns `skipped` (not `done`, not `failed`) for a skipped file; precedence `in_flight ≻ done ≻ skipped ≻ failed ≻ not_started` holds when markers co-exist.
3. **downstream-unblocks-on-skip (within enrich)** — a skipped upstream enrich stage makes the downstream enrich stage read its upstream as satisfied via `domain_completed_clause` / `ELIGIBILITY_DAG` (e.g. skip metadata → analyze eligibility no longer blocked on metadata). Scope-minimal per OQ-1: does NOT assert propose-pending inclusion.
4. **DERIV-04-covers-skipped** — the Phase-78 SQL⇔Python equivalence harness fixture matrix is extended so `skipped` is exercised on BOTH the Python `eligible()`/`stage_status` side and the SQL `eligible_clause`/`stage_status_case` side, and they agree.
5. **force-skip-not-re-enqueued-by-recovery** — a force-skipped file is NOT re-enqueued by `tasks/reenqueue.py` (both recovery paths) nor by the manual trigger endpoints (Phase-42 "UI/API/recovery must not drift").
6. **additive-only-writer keeps shadow-compare green** — the skip writer NEVER clears `analysis.failed_at` (or any failure marker); `shadow_compare` stays green with no new allowlist entry. Assert the writer is purely additive.
7. **paginated-table-no-whole-corpus-scan** — the files-table query is paginated (keyset/offset) and EXPLAIN shows it rides the partial indexes; it never scans the whole corpus per poll (PERF-01). Assert bounded row scan / index usage on a seeded corpus.
8. **terminal-analyze-retry-is-manual-only** — bulk/per-file analyze retry routes through the MANUAL retry path (respects `ELIGIBLE_AFTER_FAILURE[ANALYZE]=False`); it does not create an auto-retry loop (the 44.5K over-enqueue guard).
9. **skip-reason-sanitized-and-persisted** — the force-skip reason is run through `sanitize_pg_text` before persist (NUL/free-text footgun) and survives round-trip; empty reason is rejected (D-09 required reason).
10. **eligibility-trace-single-row** — the right-pane trace evaluates `eligible()` conjuncts for ONE file (single-row, cheap) and correctly names the unmet blocker (D-07), not a corpus query.

---

## Wave 0 Requirements

- [ ] `tests/integration/test_migrations/test_037_*.py` — migration up/down + `UNIQUE(file_id, stage)` + optional stage CHECK for the new `stage_skip` sidecar.
- [ ] Fixtures for a file with each combination of stage markers (done/failed/skipped/in_flight) to drive behaviors 1–4 — extend existing derived-status fixtures rather than build fresh.
- [ ] Extend the Phase-78 DERIV-04 equivalence fixture matrix with `skipped` rows (behavior 4).

*Existing pytest + per-bucket infrastructure covers the framework; no framework install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Pill-matrix visual legibility (light+dark, colorblind glyph cue) | UI-01 | Visual rendering not asserted by pytest | Load the files table + a selected file's right pane in both themes; confirm the 5-bucket pills (done ✓ / in_flight ● / not_started — / failed ✗ / skipped ⊘) are distinguishable by glyph, not color alone |
| Priority-inversion label clarity | PRIO-01 | Copy/UX judgment | Confirm the ▲/▼ stepper tooltip + aria-labels make "▲ raises priority = lowers the number" unambiguous |
| Orphan badge appears near the correct stage | UI-05 | Visual placement | Seed an in-flight-no-progress file; confirm the DAG-rail badge shows the count near the affected stage |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency per-bucket
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
