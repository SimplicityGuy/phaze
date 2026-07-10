---
phase: 84
slug: dedup-fingerprint-progress-cutover
status: validated
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-09
audited: 2026-07-10
gaps_found: 1
gaps_resolved: 1
gaps_escalated: 0
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
| Migration `035` contract: both statements, idempotent re-run, empty `compare_metadata` autogenerate diff, downgrade documented | SC#1 | MIG-01, MIG-02 | integration (real PG) | `just test-bucket integration` | ✅ | ✅ green |
| **Divergence test** — inconsistent corpus (marker+`analyzed` → EXCLUDED; `duplicate_resolved`+no-marker → INCLUDED) across all six readers | SC#1 | READ-04, SIDECAR-02 | integration (real PG) | `just test-bucket integration` | ✅ | ✅ green |
| **Source-scan AST guard** — `FileState.DUPLICATE_RESOLVED` in no read position in `dedup.py` (exactly 1 allowed write); zero `FileState.FINGERPRINTED` in `fingerprint.py` | SC#1, SC#2 | READ-04 | unit (DB-free, scans source) | `just test-bucket shared` | ✅ | ✅ green |
| **resolve → undo → re-resolve** — marker inserted / DELETEd / re-inserted; only `RETURNING`ed ids get `state` restored; stale replay no-ops | SC#1 | SIDECAR-02 | integration (real PG) | `just test-bucket integration` | ✅ | ✅ green |
| **`get_fingerprint_progress` real-DB test** — replaces the mock at `tests/fingerprint/services/test_fingerprint.py:291-309` | SC#2 | READ-04, DERIV-05 | integration (real PG) | `just test-bucket integration` | ✅ | ✅ green |
| **Shadow-compare integration test** — `run_shadow_compare(session).hard_fail_total == 0` through the resolve/undo cycle | SC#3 | SIDECAR-02 | integration (real PG) | `just test-bucket integration` | ✅ | ✅ green |
| **Live-corpus shadow-compare run** after `035`, before merge | SC#3 | MIG-02 | manual (see below) | `just shadow-compare --database-url <restore>` | n/a | ✅ green |

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

- [x] `tests/integration/test_migrations/test_migration_035_*.py` — mirrors `test_migration_034_backfill_cloud_awaiting.py` (bare-number revision assert, static-SQL scan, seed corpus for both reconcile directions, idempotency, empty autogenerate diff, downgrade)
- [x] Dedup **divergence test** — new file, `integration` bucket (real PG; seeds marker + `state` independently)
- [x] Dedup/fingerprint **source-scan AST guard** — DB-free, `shared` bucket
- [x] **resolve → undo → re-resolve** shadow-compare test (D-16.1) — `integration` bucket; construct `DedupResolution` via the `tests/integration/test_shadow_compare.py:157` idiom
- [x] **Replace** `tests/fingerprint/services/test_fingerprint.py:291-309` with a real-DB test (D-15) — moves out of the mock-only `fingerprint` bucket into `integration`; update `tests/buckets.json` if bucket membership changes
- [x] No framework install needed (pytest / pytest-asyncio already present)

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live-corpus repair proven (D-16.2) | MIG-02, SIDECAR-02 | CI's synthetic corpus cannot contain real production rows. **DONE 2026-07-10** — see `84-06-SUMMARY.md`. | Executed **read-only against the live database**, no snapshot: every statement inside `BEGIN TRANSACTION READ ONLY` (`SHOW transaction_read_only` → `on`). Corrections: the `_test`-suffix "destructive-write guard" the plan cited **does not exist**; `shadow_compare` has zero write calls, so no restore was ever required. Result: production is at Alembic `031`, `dedup_resolution` absent, **0** `duplicate_resolved` files — the invariant has zero exposure and `035` is a no-op there. |
| `completed` jumps / `failed` drops | READ-04, DERIV-05 | Magnitude is a property of the live corpus, not any fixture. **MEASURED 2026-07-10.** | `fingerprint_results` is **empty** in production, so old contract and new contract both report `completed = 0`, `failed = 0`. The cutover is still the fix (D-11); this corpus has no fingerprint data to expose the delta. Recorded in `84-06-SUMMARY.md` so a future reader does not expect a jump that cannot occur.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or a Wave 0 dependency
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references above
- [x] No watch-mode flags
- [x] Feedback latency < 90s per bucket
- [x] **Every guard mutation-tested in both directions** (false-negative *and* false-positive), per the mutation table above
- [x] Every new test passes via `just test-bucket <bucket>` **in isolation**
- [x] Live-corpus `shadow-compare` run recorded with `hard_fail_total=0`
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-10


---

## Surfaces added after the plan was written

These were not in the original strategy. Each was found by a downstream gate and is mutation-verified.

| Surface | Found by | Test | Mutation → RED |
|---------|----------|------|----------------|
| Router-level fingerprint-progress contract | post-merge gate | `tests/fingerprint/routers/test_pipeline_fingerprint.py::test_fingerprint_progress_returns_counts` | revert endpoint to `state == FINGERPRINTED` |
| `undo_resolve` validate-before-DELETE (WR-01) | code review | `test_undo_with_invalid_previous_state_keeps_marker_and_gate_green` | pre-fix `dedup.py` (`a67ed16a`) |
| `undo_resolve` malformed-UUID handling (WR-02) | code review | `test_undo_with_malformed_uuid_does_not_raise` | pre-fix `dedup.py` |
| `undo_resolve` duplicate-entry count | code review | `test_undo_duplicate_entries_do_not_inflate_count` | pre-fix `dedup.py` |
| Agent-worker import boundary (T-84-04-01) | secure-phase | `tests/shared/test_fingerprint_import_boundary.py` | hoist a DB import to module scope → both tests RED |
| Concurrent double-submit (T-84-03-03) | secure-phase | `test_concurrent_double_submit_insert_conflict_is_a_noop` | drop `.on_conflict_do_nothing(...)` |
| `shadow_compare` read-only (T-84-06-02) | secure-phase auditor | `tests/shared/test_shadow_compare_readonly.py` | import `delete` / add `session.add(...)` |
| T-84-03-02 branch coverage | validate-phase | `test_undo_accepts_uuid_typed_id`, `test_undo_with_null_previous_state_keeps_marker` | drop the UUID branch / drop `except ValueError` |

**Note — one branch is not a guard.** `dedup.py`'s `isinstance(raw_state, str)` check survives every
mutation: `FileState` is a `StrEnum`, so members already satisfy `isinstance(x, str)`, and
`FileState(None | 42 | [...] | True)` raises `ValueError` regardless. It exists only to narrow
`Any | None` → `str` for mypy. The load-bearing control is the surrounding `try/except ValueError`,
and removing *that* turns both tests RED. Recorded rather than papered over.

---

## Coverage

Measured over `tests/integration` + `tests/discovery` + `tests/shared` + `tests/fingerprint`:

| Module | Coverage |
|--------|----------|
| `src/phaze/services/dedup.py` | **100.00%** |
| `src/phaze/services/stage_status.py` | **100.00%** |
| `src/phaze/services/fingerprint.py` | 93.92% (misses at `104-117`, `219-221` are outside `get_fingerprint_progress` and pre-date this phase) |

Project gate is `fail_under = 95` on the **combined** number (`coverage-combine`), not per-module.

---

## Validation Audit 2026-07-10

| Metric | Count |
|--------|-------|
| Gaps found | 1 |
| Resolved | 1 |
| Escalated | 0 |

**Gap:** `services/dedup.py:315,325` — two uncovered branches inside the T-84-03-02 undo-payload
validation, which is attacker-reachable through the browser-held `[{id, previous_state}]` payload.
Closed by two tests; `dedup.py` reached 100%.

**All six required mutations were re-verified independently during this audit**, not accepted on the
executors' word. Four of them (divergence-reader revert, `pg_insert` writer deletion, `completed`
revert, `failed` row-count revert) had existed only as claims in SUMMARY files until this run. All
four turned RED as specified, and the source was restored clean each time.

`nyquist_compliant: true` — earned, not asserted.
