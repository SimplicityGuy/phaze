---
phase: 60
slug: review-apply
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-01
---

# Phase 60 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
>
> Post-execution audit (2026-07-01): **5/5 requirements COVERED** by named, green automated tests.
> No gaps — `nyquist_compliant: true`.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio + httpx AsyncClient |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest tests/test_review_apply_workspaces.py -x -q` |
| **Full suite command** | `just integration-test` (self-contained ephemeral Postgres+Redis; sets `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL`/`PHAZE_REDIS_URL`) |
| **Estimated runtime** | ~4s (targeted) · ~6min (full suite) |

> Env note: the phase-60 suite needs the ephemeral Postgres on port 5433 (`just test-db`).
> Do NOT set `PHAZE_QUEUE_URL=redis` — the SAQ queue tests derive their Postgres DSN from `TEST_DATABASE_URL`.

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_review_apply_workspaces.py -x -q`
- **After every plan wave:** Run the targeted suite + `tests/integration/test_review_audit.py`
- **Before `/gsd:verify-work`:** Full suite green (≥85% coverage)
- **Max feedback latency:** ~4 seconds (targeted)

---

## Per-Task Verification Map

Each REVIEW requirement maps to named automated tests. All GREEN (targeted suite: 19 passed, 0 xfailed).

| Task ID | Plan | Wave | Requirement | Threat Ref | Test(s) | Test Type | Status |
|---------|------|------|-------------|------------|---------|-----------|--------|
| 60-02/03 | 02, 03 | 2-3 | REVIEW-01 | T-60-XSS, T-60-R6 | `test_diff_row_before_after`, `test_edit_patch_targets_own_row`, `test_diff_row_edit_island_is_js_context_safe`, `test_tagwrite_workspace_apply_and_bulk_wiring`, `test_review_fragments_are_bare` | unit (route+template) | ✅ green |
| 60-01 | 01, 03 | 1, 3 | REVIEW-02 | T-60-01 | `test_bulk_approve_high_confidence_server_predicate`, `test_tag_bulk_no_discrepancy_predicate`, `test_tagwrite_workspace_apply_and_bulk_wiring` | unit (behavioral) | ✅ green |
| 60-04 T1 | 04 | 4 | REVIEW-03 | — | `test_dedupe_keeper_resolve_wiring` | unit | ✅ green |
| 60-04 T2 | 04 | 4 | REVIEW-04 | T-60-CUE | `test_cue_gate_and_preview` | unit | ✅ green |
| 60-01/04 | 01, 04 | 1, 4 | REVIEW-05 | T-60-03 | `test_review_audit_one_row`; `test_tag_write_produces_exactly_one_audit_row`, `test_tag_undo_reapplies_before_tags`, `test_tag_undo_missing_log_returns_404`, `test_dedupe_resolve_one_resolution_and_undo_round_trips` (`tests/integration/test_review_audit.py`) | integration | ✅ green |
| — (cross-cut) | 01-04 | 1-4 | R-2 / R-5 | T-57-01, T-60-DOS | `test_review_fragments_are_bare` (bare-fragment), `test_review_single_poll_discipline` (counts-only, no 2nd poll) | unit (guard) | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [x] Test scaffold + seed factories (`tests/test_review_apply_workspaces.py`, conftest `make_file`/`seed_pending_proposal`/`seed_executed_file_with_metadata`/`seed_duplicate_group`/`seed_cue_set`) — created in Plan 60-01 Task 1
- [x] Server re-query assertions for the NEW thin endpoints (D-02 bulk-approve, D-03 tag-bulk) — forged id-list has no effect
- [x] Fragment/template render + single-poll guards for the six superseded workspace stages
- [x] Audit-integrity integration tests (`tests/integration/test_review_audit.py`)

*Existing pytest infrastructure covered the framework; the two new test files target the new endpoints + workspace fragments.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Visual before→after diff styling (struck-through current vs highlighted proposed) | REVIEW-01 | Pixel/visual fidelity to UI-SPEC not automatable in pytest | Load `/s/rename`, confirm diff partial matches 60-UI-SPEC.md |
| Counts-only OOB poll never clobbers in-progress selection | REVIEW-02 | Live HTMX polling + operator-selection race is a runtime-timing behavior | Select rows, wait ≥5s for `/pipeline/stats` poll, confirm selection subtree unchanged |

*The structural halves of both (autoescape/`|tojson` safety, no-second-poll discipline) ARE automated — `test_diff_row_edit_island_is_js_context_safe` and `test_review_single_poll_discipline`. Only pixel-fidelity and live-race timing remain manual.*

---

## Validation Sign-Off

- [x] All tasks have automated verify (no Wave 0 stubs remain — all xfails converted)
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (none remaining)
- [x] No watch-mode flags
- [x] Feedback latency < 60s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** verified 2026-07-01

---

## Validation Audit 2026-07-01

| Metric | Count |
|--------|-------|
| Requirements | 5 |
| COVERED | 5 |
| PARTIAL | 0 |
| MISSING | 0 |
| Gaps found | 0 |
| Tests generated this audit | 0 (already fully covered) |

State A audit (VALIDATION.md existed). Reconciled the per-task map against the shipped test files
(`tests/test_review_apply_workspaces.py` — 12 tests; `tests/integration/test_review_audit.py` — 4 tests).
Every REVIEW-01..05 requirement maps to ≥1 named, green automated test. No gaps → no gsd-nyquist-auditor
spawn needed.
