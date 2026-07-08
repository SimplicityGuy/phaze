---
phase: 79
slug: shadow-compare-gate-live-corpus
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-08
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

*(Task IDs finalized by the planner; this map is the sampling skeleton the planner fills as plans are written.)*

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 79-01-xx | 01 | 1 | MIG-02 | — | Shared assertion core returns per-invariant divergent count + capped sample file_ids; no I/O side effects | integration | `uv run pytest tests/integration/test_shadow_compare.py -k core` | ❌ W0 | ⬜ pending |
| 79-01-xx | 01 | 1 | MIG-02 | — | Every FileState value (§6.1) has an implication assertion or is documented vacuous (DISCOVERED); allowlist = {FINGERPRINTED, LOCAL_ANALYZING} counted-not-failed | integration | `uv run pytest tests/integration/test_shadow_compare.py -k invariants` | ❌ W0 | ⬜ pending |
| 79-02-xx | 02 | 2 | MIG-02 | — | `python -m` runner + `just shadow-compare` exit nonzero on hard-fail divergence, zero on clean; `--verbose` dumps full set | integration | `uv run pytest tests/integration/test_shadow_compare.py -k cli` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/integration/test_shadow_compare.py` — new DB-backed test file (reuse the `db_session` fixture + per-table seed helpers from `tests/integration/test_stage_status_equivalence.py`)
- [ ] Fixture corpus builder seeding one `FileRecord` + its output rows per FileState value (incl. both allowlisted soft cases) so all ~17 invariants + vacuous DISCOVERED are exercised hermetically

*Existing pytest + real-PG `db_session` infrastructure (Phase 78) covers the framework; no install needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Gate passes on a restore of the live 200K corpus after the `032` backfill | MIG-02 (SC-3) | Deferred to homelab rollout per CONTEXT D-02 — no live corpus dump available to this worktree | On next rollout: `pg_restore` live corpus into a scratch DB → `just shadow-compare` (or point the runner at the restore DSN) → record the per-invariant output + pass/fail in VERIFICATION |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references (the new test file + fixture builder)
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
