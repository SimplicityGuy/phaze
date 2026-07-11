---
phase: 87
slug: operator-ui-stage-matrix-failure-retry-eligibility-trace-pri
status: approved
nyquist_compliant: true
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

## Critical-Behavior → Plan Coverage Map

> Traced against the 8 committed plans during plan-phase verification (plan-checker, 2026-07-11). 10/10 critical behaviors map to an automated test in a specific plan.

| Behavior | Owning Plan(s) | Test Home |
|----------|----------------|-----------|
| 1 skipped-file-leaves-pending-set | 87-03 | pending-set exclusion tests (3 enrich stages) |
| 2 skipped-reads-as-distinct-bucket | 87-02, 87-03 | `stage_status_case` precedence tests |
| 3 skip-orthogonal-across-enrich (corrected) | 87-03 | orthogonality assertion (subsumed by 1 & 5) |
| 4 DERIV-04-covers-skipped | 87-03 | extended `test_stage_status_equivalence.py` fixtures |
| 5 force-skip-not-re-enqueued-by-recovery | 87-03 | recovery/re-enqueue exclusion + manual-trigger tests |
| 6 additive-only-writer keeps shadow-green | 87-03, 87-06 | shadow-compare green + writer additive-only assertion |
| 7 paginated-table-no-whole-corpus-scan | 87-04 | EXPLAIN / bounded-LIMIT test on seeded corpus |
| 8 terminal-analyze-retry-is-manual-only | 87-07 | analyze retry manual-path (no auto-loop) test |
| 9 skip-reason-sanitized-and-persisted | 87-06 | `sanitize_pg_text` round-trip + empty-reason reject |
| 10 eligibility-trace-single-row | 87-06 | single-row conjunct trace + named-blocker test |

---

## Critical Behaviors (Nyquist sample points — MUST be automated)

Derived from RESEARCH §Validation Architecture. Each is a correctness-load-bearing behavior of the `skipped`-marker slice or the paginated surface:

1. **skipped-file-leaves-pending-set** — a file with a `skipped` marker for stage S is ABSENT from `get_metadata_pending_files` / `get_fingerprint_pending_files` / `get_discovered_files_with_duration` for S (via `~skipped` in `eligible_clause`). One test per enrich stage.
2. **skipped-reads-as-distinct-bucket** — `stage_status_case(S)` returns `skipped` (not `done`, not `failed`) for a skipped file; precedence `in_flight ≻ done ≻ skipped ≻ failed ≻ not_started` holds when markers co-exist.
3. **skip-marker-is-orthogonal-across-enrich-stages** — CORRECTED: `ELIGIBILITY_DAG[METADATA] = [ANALYZE] = [FINGERPRINT] = ()` (enums/stage.py:61-69), so enrich stages have NO mutual upstream dependency — a skip on one enrich stage neither blocks nor unblocks another. The real load-bearing guarantees of the skip marker are covered by behaviors 1 (leaves its own pending set) and 5 (not re-enqueued by recovery); there is no within-enrich cross-stage unblocking to test. Scope-minimal per OQ-1: force-skip does NOT feed the propose pending set (deferred to Phase 90). Assert the orthogonality: a skip on stage S leaves stages ≠ S in enrich unchanged.
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

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency per-bucket
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-11
