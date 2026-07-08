---
phase: 79
slug: shadow-compare-gate-live-corpus
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-08
validated: 2026-07-08
---

# Phase 79 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Detailed Validation Architecture lives in `79-RESEARCH.md`; this file is the executable sampling contract.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x (pytest-asyncio) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/integration/test_shadow_compare.py` |
| **Full suite command** | `just test-bucket integration` |
| **Estimated runtime** | ~30–90 seconds (DB-backed; needs `:5433` ephemeral PG) |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/integration/test_shadow_compare.py`
- **After every plan wave:** Run `just test-bucket integration` (proves in-isolation, per CLAUDE.md)
- **Before `/gsd:verify-work`:** Full suite must be green + `uv run ruff check .` + `uv run mypy .`
- **Max feedback latency:** 90 seconds

**DB requirement:** DB-backed tests need `TEST_DATABASE_URL` / `PHAZE_QUEUE_URL` pointed at the `:5433` ephemeral DB (conftest defaults to `:5432`).

---

## Per-Task Verification Map

*(Post-execution audit 2026-07-08: task IDs and command selectors finalized against the shipped `tests/integration/test_shadow_compare.py`. The plan-time `-k invariants` selector matched zero node ids; the coverage it named — full FileState coverage + the {fingerprinted, local_analyzing} allowlist — is the `-k core` registry cell, so the command is corrected here.)*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 79-01-T2 | 01 | 1 | MIG-02 | T-79-01, T-79-02 | Shared assertion core returns per-invariant divergent count + capped sample file_ids; no I/O side effects | integration | `uv run pytest tests/integration/test_shadow_compare.py -k "divergent or consistent or report_shape"` | ✅ | ✅ green (29 passed) |
| 79-01-T2 | 01 | 1 | MIG-02 | T-79-03 | Every FileState value (§6.1) has an implication assertion or is documented vacuous (DISCOVERED); allowlist = {FINGERPRINTED, LOCAL_ANALYZING} counted-not-failed | integration | `uv run pytest tests/integration/test_shadow_compare.py -k core` | ✅ | ✅ green (1 passed) |
| 79-02-T2 | 02 | 2 | MIG-02 | T-79-04, T-79-05 | `python -m` runner + `just shadow-compare` exit nonzero on hard-fail divergence, zero on clean; `--verbose` dumps full set | integration | `uv run pytest tests/integration/test_shadow_compare.py -k cli` | ✅ | ✅ green (2 passed) |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*
*Requires the `:5433` ephemeral DB with `TEST_DATABASE_URL`/`MIGRATIONS_TEST_DATABASE_URL` exported (`just test-db`); full-file run = 34 cells / 130 in the `integration` bucket.*

---

## Wave 0 Requirements

- [x] `tests/integration/test_shadow_compare.py` — DB-backed test file shipped (reuses the `db_session` fixture + per-table seed helpers from `tests/integration/test_stage_status_equivalence.py`; adds a module-level `_test`-DB safety guard, CR-01)
- [x] Fixture corpus seeds one `FileRecord` + its output rows per FileState value (incl. both allowlisted soft cases), parametrized over `HARD_INVARIANTS` so all 16 invariants + vacuous DISCOVERED are exercised hermetically

*Existing pytest + real-PG `db_session` infrastructure (Phase 78) covered the framework; no install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Gate passes on a restore of the live 200K corpus after the `032` backfill | MIG-02 (SC-3) | Deferred to homelab rollout per CONTEXT D-02 — no live corpus dump available to this worktree | On next rollout: `pg_restore` live corpus into a scratch DB → `just shadow-compare` (or point the runner at the restore DSN) → record the per-invariant output + pass/fail in VERIFICATION |

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify (2 tasks/wave)
- [x] Wave 0 covers all MISSING references (new test file + fixture builder covered by 79-01-PLAN.md Task 2)
- [x] No watch-mode flags
- [x] Feedback latency < 90s
- [x] `nyquist_compliant: true` set in frontmatter

*Note: `wave_0_complete` stays `false` until execution actually writes `tests/integration/test_shadow_compare.py` + the fixture builder; the sign-off boxes reflect plan-time compliance (a plan covers every Wave-0 reference), flipped post-planning by convention.*

**Approval:** approved 2026-07-08 (plan-time)

---

## Validation Audit 2026-07-08 (post-execution)

| Metric | Count |
|--------|-------|
| Automated behaviors audited | 3 |
| Resolved (green) | 3 |
| Escalated | 0 |
| Manual-only (deferred) | 1 (live 200K-corpus run, SC-3, per D-02) |

All three plan-time automated behaviors are COVERED and green against the shipped
`tests/integration/test_shadow_compare.py` (34 cells; per-selector: 29 / 1 / 2 passed). The only
correction was the imprecise `-k invariants` selector → `-k core`. No test-generation gaps; the
`gsd-nyquist-auditor` was not needed. `wave_0_complete` flipped to `true`. The sole outstanding item
is the Manual-Only live-corpus run, which is deferred to the homelab rollout by CONTEXT decision D-02
and tracked here + in `79-HUMAN-UAT.md`.
