---
phase: 59
slug: identify-workspaces
status: verified
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-30
---

# Phase 59 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (pytest-asyncio) |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_identify_workspaces.py` |
| **Full suite command** | `uv run pytest --cov --cov-report=term-missing` |
| **Estimated runtime** | ~4s (phase file, 14 tests) · ~6m (full suite, 2575 tests) |
| **Test-DB env** | `TEST_DATABASE_URL=…@localhost:5433/phaze_test`, `MIGRATIONS_TEST_DATABASE_URL=…/phaze_migrations_test`, `PHAZE_REDIS_URL=redis://localhost:6380/0` (ephemeral containers; conftest default 5432 is not running) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_identify_workspaces.py`
- **After every plan wave:** Run `uv run pytest --cov --cov-report=term-missing`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** ~4s (quick) / ~6m (full)

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 59-01-01 | 01 | 1 | IDENT-01, IDENT-02 | — | Wave-0 test surface (bare-fragment R-5 + single-poll WORK-05 guards; IDENT behavior stubs) | integration | `uv run pytest tests/test_identify_workspaces.py -x` | ✅ | ✅ green |
| 59-01-02 | 01 | 1 | IDENT-01, IDENT-02 | T-59-DOS / T-59-INJ / T-59-SCOPE | `get_trackid_stage_files` / `get_tracklist_set_rows` read-only + SAVEPOINT-degrade-safe; pure ORM | unit | `uv run pytest tests/test_identify_workspaces.py -x && uv run mypy src/phaze/services/pipeline.py` | ✅ | ✅ green |
| 59-02-01 | 02 | 2 | IDENT-01 | T-59-XSS | Track-ID table autoescaped; bare fragment; single poll | integration | `uv run pytest tests/test_identify_workspaces.py::test_identify_fragments_are_bare tests/test_identify_workspaces.py::test_identify_single_poll_discipline -x` | ✅ | ✅ green |
| 59-02-02 | 02 | 2 | IDENT-01 | T-57-01 | `STAGE_PARTIALS['trackid']` static literal; dead-template guard green; done⟺`status=="success"` (Pitfall 1) | integration | `uv run pytest tests/test_identify_workspaces.py tests/test_dead_template_guard.py tests/test_shell_routes.py -x && uv run mypy src/phaze/routers/shell.py` | ✅ | ✅ green |
| 59-03-01 | 03 | 3 | IDENT-02 | T-59-XSS / T-59-OVERENQ | Tracklist cells autoescaped; R-4 trigger guard; single poll | integration | `uv run pytest tests/test_identify_workspaces.py::test_identify_single_poll_discipline -x` | ✅ | ✅ green |
| 59-03-02 | 03 | 3 | IDENT-02 | T-57-01 | `STAGE_PARTIALS['tracklist']` static literal; 3 step cards + per-set N/M coverage (latest version only) | integration | `uv run pytest tests/test_identify_workspaces.py tests/test_dead_template_guard.py tests/test_shell_routes.py -x && uv run mypy src/phaze/routers/shell.py` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

**Requirement coverage:** IDENT-01 → COVERED (`test_trackid_table_signals`, `test_trackid_success_renders_done` + 4 `get_trackid_stage_files_*` unit tests). IDENT-02 → COVERED (`test_tracklist_step_cards_and_triggers`, `test_tracklist_per_set_coverage` + 4 `get_tracklist_set_rows_*` unit tests, incl. `test_get_tracklist_set_rows_counts_latest_version_only` — the WR-01 multi-version regression). Cross-cutting R-5/WORK-05 invariants → COVERED (`test_identify_fragments_are_bare`, `test_identify_single_poll_discipline`). 14/14 green.

---

## Wave 0 Requirements

- [x] `tests/test_identify_workspaces.py` — mirrors `tests/test_enrich_analyze_workspaces.py`; IDENT-01/IDENT-02 behavior tests + helper unit tests (14 tests)
- [x] reuse existing `tests/conftest.py` shared fixtures

*Existing infrastructure covers all phase requirements — no new framework install.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Visual conformance to 59-UI-SPEC (spacing/color/type/copy) | IDENT-01, IDENT-02 | UI-SPEC visual fidelity is verified by `/gsd:ui-review`, not unit tests | Render `/s/trackid` and `/s/tracklist`, compare to 59-UI-SPEC Patterns A/B/C |

*All automated-verifiable behaviors have automated tests; only pixel-level visual fidelity is manual.*

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references (no MISSING — every requirement COVERED)
- [x] No watch-mode flags
- [x] Feedback latency < 6m (full) / < 4s (quick)
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-01

---

## Validation Audit 2026-07-01

| Metric | Count |
|--------|-------|
| Requirements | 2 (IDENT-01, IDENT-02) |
| Gaps found | 0 |
| Resolved | 0 (no gaps — all COVERED at execution) |
| Escalated to manual-only | 1 (UI-SPEC pixel fidelity — not automatable) |

State A audit: VALIDATION.md was seeded at plan time (stub). All 6 execution tasks shipped with green automated verify; both requirements COVERED by 14 passing tests. No auditor pass required (Step 3 short-circuit — zero gaps). `nyquist_compliant: true`.
