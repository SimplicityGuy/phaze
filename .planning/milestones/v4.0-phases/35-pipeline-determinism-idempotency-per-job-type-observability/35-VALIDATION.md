---
phase: 35
slug: pipeline-determinism-idempotency-per-job-type-observability
status: nyquist_compliant
nyquist_compliant: true
wave_0_complete: true
created: 2026-06-12
---

# Phase 35 — Validation Strategy

> Per-phase validation contract. Reconstructed from artifacts after execution (State B).
> Every phase requirement maps to a dedicated, green automated test. No gaps to fill.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x + pytest-asyncio |
| **Config file** | `pyproject.toml` (`[tool.pytest.ini_options]`) |
| **Quick run command** | `uv run pytest <file>::<test>` |
| **Full suite command** | `just integration-test` (ephemeral Postgres :5433 + Redis :6380, auto-teardown) |
| **Estimated runtime** | ~180 seconds (1709 tests) |

---

## Sampling Rate

- **After every task commit:** Run the touched module's `uv run pytest <file>`
- **After every plan wave:** Run `just integration-test`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** ~180 seconds

---

## Per-Requirement Verification Map

| Requirement | Plan(s) | Threat Ref | Secure Behavior | Test File(s) | Key Tests | Status |
|-------------|---------|------------|-----------------|--------------|-----------|--------|
| **SCHED** — schedulability without duplicate queue items | 35-01 | T-35-01 | Central `before_enqueue` hook keys every registered task `<fn>:<natural_id>` unconditionally; no call site can drift to random uuid keys | `tests/test_deterministic_key.py` (17), `tests/test_pipeline_counters.py` (4) | `test_every_routable_task_is_keyed_or_exempt` (drift-guard), per-function key-builder tests, counter INCR/read | ✅ green |
| **IDEMP** — idempotent re-runs (no duplicate rows) | 35-02 | T-35-04, T-35-05, T-35-06 | Partial-index upsert overwrites only the PENDING proposal; approvals structurally protected; migration dedupes before adding the unique index | `tests/test_proposals_upsert.py` (5), `tests/test_migration_019_dedupe.py` (2), `tests/test_services/test_proposal.py` | `test_double_run_overwrites_single_pending_row`, `test_rerun_never_touches_approved_row`, `test_out_of_range_file_index_is_skipped` (WR-01), `test_rerun_does_not_regress_terminal_file_state` (WR-04), migration dedupe round-trip | ✅ green |
| **MANUAL-META** — operator-controlled metadata extraction | 35-01 | T-35-03 | Both auto-enqueue paths removed; manual trigger is the sole producer and builds the COMPLETE `ExtractMetadataPayload` | `tests/test_no_auto_metadata_enqueue.py` (2), `tests/test_routers/test_scan.py` (8), `tests/test_routers/test_pipeline.py` (38) | `test_agent_upsert_does_not_enqueue_metadata`, `test_legacy_scan_does_not_enqueue_metadata`, `test_trigger_scan_does_not_auto_enqueue_extract`, `test_extract_metadata_enqueues_complete_payload` (CR-01) | ✅ green |
| **OBSERV** — per-job-type pipeline observability | 35-03, 35-04, 35-05 | T-35-07, T-35-09, T-35-10, T-35-11 | DB-truth per-stage reconcile (`COUNT(DISTINCT)` per output table, failure-isolated); reconcile wired into dashboard + 5s poll (never 500s); honest DAG canvas with int-only XSS-safe interpolation | `tests/test_stage_progress.py` (9), `tests/test_pipeline_dag_context.py` (12), `tests/test_dag_canvas_render.py` (23), `tests/test_pipeline_counters.py` (4) | `test_analyzed_but_no_metadata_counts_independently`, `test_build_dag_context_never_raises_on_counter_outage`, `test_topology_edge_list_is_honest`, `test_topology_column_one_chips_do_not_overlap` (UAT), `test_proposals_batch_counter_is_not_a_fallback_done` (WR-03) | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements — no new framework or fixtures needed.
Every plan shipped its tests alongside implementation (TDD-style RED/GREEN commits visible in
the phase git history). The fingerprint trigger also gained a complete-payload regression test
(`tests/test_routers/test_pipeline_fingerprint.py::test_trigger_fingerprint_enqueues_complete_payload`,
CR-02) during code-review remediation.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Live agent-worker consumes a metadata/fingerprint job without dead-lettering | MANUAL-META / OBSERV | Requires a connected file-server agent worker running `model_validate` against the real payload; no agent is connected in CI/dev. Payload COMPLETENESS is automated (`test_extract_metadata_enqueues_complete_payload` / `test_trigger_fingerprint_enqueues_complete_payload`); only the live round-trip is manual. | After homelab redeploy: trigger "Extract Metadata" / "Fingerprint" from the DAG canvas; confirm in the SAQ admin UI (`/saq`) that jobs complete (no ValidationError dead-letters). Tracked in `35-HUMAN-UAT.md` item 4. |
| DAG canvas visual layout, reactive gating, < sm responsive fallback | OBSERV | Pixel layout / Alpine reactivity / Tailwind breakpoints render only in a real browser | Verified 2026-06-12 via Playwright-MCP (see `35-HUMAN-UAT.md` items 1-3, all pass; chip-overlap defect found and fixed in commit 88881ab + guarded by `test_topology_column_one_chips_do_not_overlap`). |

---

## Validation Audit 2026-06-12

| Metric | Count |
|--------|-------|
| Requirements | 4 (SCHED, IDEMP, MANUAL-META, OBSERV) |
| Covered (automated) | 4 |
| Partial | 0 |
| Missing | 0 |
| Manual-only residual | 2 (live-worker round-trip; browser visual — both have automated proxies) |

**Verdict: NYQUIST-COMPLIANT.** All four phase requirements have automated, green verification.
The two manual-only items are inherent environment limitations (live distributed agent; browser
rendering), each backed by an automated proxy test (payload-schema validation; topology +
overlap render tests). Full suite green: 1709 passed.
