---
phase: 84
slug: dedup-fingerprint-progress-cutover
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-07-09
---

# Phase 84 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Derived from `84-RESEARCH.md` § Validation Architecture. Decisions D-14, D-15, D-16 are locked in `84-CONTEXT.md`.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + pytest-asyncio (`uv run pytest`) |
| **Config file** | `pyproject.toml`; buckets in `tests/buckets.json` |
| **Quick run command** | `just test-bucket <bucket>` (DB buckets run serial) |
| **Full suite command** | `just integration-test` (ephemeral PG :5433 + Redis :6380, auto-teardown) |
| **Migration test command** | `MIGRATIONS_TEST_DATABASE_URL=postgresql+asyncpg://…@localhost:5433/phaze_migrations_test just test-bucket integration` |
| **Estimated runtime** | ~90s per DB bucket; ~6min full integration |

**Isolation hazard (repo-documented):** parallel per-bucket CI exposes non-hermetic tests — `get_settings` lru_cache leakage and `saq_jobs` stub poisoning. Every new test must pass via `just test-bucket <bucket>` *in isolation*, not only in a full-suite run.

**Migration-test footgun:** `MIGRATIONS_TEST_DATABASE_URL` defaults to port 5432, but `just test-db` provisions 5433, and `just test-bucket` does not export it. Export both DB URLs or the migration test fails in isolation in a way that mimics the colima flake.

---

## Sampling Rate

- **After every task commit:** Run the owning bucket — `just test-bucket <bucket>`
- **After every plan wave:** Run `just integration-test`
- **Before `/gsd:verify-work`:** Full suite green + every guard mutation-tested (see Sign-Off)
- **Max feedback latency:** ~90 seconds (single bucket)

**Mutation-test discipline (standing rule, `feedback_mutation_test_guard_tests`):** a green guard proves nothing. Every guard this phase ships is mutation-tested before the phase closes — break the source, watch it go RED, restore. Phase 83 shipped two toothless guards; do not repeat it.

---

## Per-Task Verification Map

Plan/task IDs are assigned by the planner. This map fixes the *surfaces* each success criterion must be sampled at; the planner binds task IDs to rows.

| Surface | SC | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|----|-------------|-----------|-------------------|-------------|--------|
| Migration `035` contract: both statements, idempotent re-run, empty `compare_metadata` autogenerate diff, downgrade documented | SC#1 | MIG-01, MIG-02 | integration (real PG) | `just test-bucket integration` | ❌ W0 | ⬜ pending |
| **Divergence test** — inconsistent corpus (marker+`analyzed` → EXCLUDED; `duplicate_resolved`+no-marker → INCLUDED) across all six readers | SC#1 | READ-04, SIDECAR-02 | integration (real PG) | `just test-bucket integration` | ❌ W0 | ⬜ pending |
| **Source-scan AST guard** — `FileState.DUPLICATE_RESOLVED` in no read position in `dedup.py` (exactly 1 allowed write); zero `FileState.FINGERPRINTED` in `fingerprint.py` | SC#1, SC#2 | READ-04 | unit (DB-free, scans source) | `just test-bucket shared` | ❌ W0 | ⬜ pending |
| **resolve → undo → re-resolve** — marker inserted / DELETEd / re-inserted; only `RETURNING`ed ids get `state` restored; stale replay no-ops | SC#1 | SIDECAR-02 | integration (real PG) | `just test-bucket integration` | ❌ W0 | ⬜ pending |
| **`get_fingerprint_progress` real-DB test** — replaces the mock at `tests/fingerprint/services/test_fingerprint.py:291-309` | SC#2 | READ-04, DERIV-05 | integration (real PG) | `just test-bucket integration` | ❌ W0 | ⬜ pending |
| **Shadow-compare integration test** — `run_shadow_compare(session).hard_fail_total == 0` through the resolve/undo cycle | SC#3 | SIDECAR-02 | integration (real PG) | `just test-bucket integration` | ❌ W0 | ⬜ pending |
| **Live-corpus shadow-compare run** after `035`, before merge | SC#3 | MIG-02 | manual (see below) | `just shadow-compare --database-url <restore>` | n/a | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

### Required mutations (each must turn its guard RED)

| Guard | Mutation | Expected |
|-------|----------|----------|
| Divergence test | Revert any one reader's `~dedup_resolved_clause()` → `FileRecord.state != FileState.DUPLICATE_RESOLVED` | RED — File A wrongly included, File B wrongly excluded |
| Source scan (false negative) | Reintroduce a `FileState.DUPLICATE_RESOLVED` read in a positional `.where(a, b, c)` arg | RED |
| Source scan (false positive) | Leave the surviving dual-writer `f.state = FileState.DUPLICATE_RESOLVED` at `dedup.py:268` untouched | GREEN — the writer is allowed |
| `get_fingerprint_progress` test | Revert `completed` to `state == FileState.FINGERPRINTED` | RED |
| `get_fingerprint_progress` test | Revert `failed` to a **row** count over `fingerprint_results` | RED — two-engine-failure file adds 2, not 1 |
| Shadow-compare test | Delete the `pg_insert` writer from `resolve_group` | RED — `hard_fail_total > 0` |

---

## Wave 0 Requirements

- [ ] `tests/integration/test_migrations/test_migration_035_*.py` — mirrors `test_migration_034_backfill_cloud_awaiting.py` (bare-number revision assert, static-SQL scan, seed corpus for both reconcile directions, idempotency, empty autogenerate diff, downgrade)
- [ ] Dedup **divergence test** — new file, `integration` bucket (real PG; seeds marker + `state` independently)
- [ ] Dedup/fingerprint **source-scan AST guard** — DB-free, `shared` bucket
- [ ] **resolve → undo → re-resolve** shadow-compare test (D-16.1) — `integration` bucket; construct `DedupResolution` via the `tests/integration/test_shadow_compare.py:157` idiom
- [ ] **Replace** `tests/fingerprint/services/test_fingerprint.py:291-309` with a real-DB test (D-15) — moves out of the mock-only `fingerprint` bucket into `integration`; update `tests/buckets.json` if bucket membership changes
- [ ] No framework install needed (pytest / pytest-asyncio already present)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live-corpus repair proven (D-16.2) | MIG-02, SIDECAR-02 | CI's synthetic corpus cannot contain the real post-`032` `state='duplicate_resolved'`-without-marker rows that D-01 discovered. Only a run against a production restore proves `035` covered them. Phase 79 built this gate re-runnable but deferred the live run — which is exactly why D-01 went unnoticed. | 1. Restore a live DB snapshot. 2. `alembic upgrade head` (applies `035`). 3. `just shadow-compare --database-url <restore-dsn>`. 4. Assert exit code `0` and `TOTALS: hard_fail_total=0`; the `duplicate_resolved` invariant line reads `0 divergent`. 5. Record counts in the phase SUMMARY. |
| `completed` jumps / `failed` drops | READ-04, DERIV-05 | The magnitude of the change is a property of the live corpus, not of any fixture. It is the **fix**, not a regression (D-11): `completed` currently reads `state == FINGERPRINTED`, written only by `retry_analysis_failed`, so it counts ≈nothing; `failed` currently double-counts per-engine rows. | Before/after `GET /api/v1/fingerprint/progress` (or `just` curl recipe, `justfile:500`) against the restore. Record both readings in the SUMMARY so the delta is not read as breakage. |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or a Wave 0 dependency
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references above
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s per bucket
- [ ] **Every guard mutation-tested in both directions** (false-negative *and* false-positive), per the mutation table above
- [ ] Every new test passes via `just test-bucket <bucket>` **in isolation**
- [ ] Live-corpus `shadow-compare` run recorded with `hard_fail_total=0`
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
