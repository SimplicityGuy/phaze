---
phase: 34-pipeline-queue-depth-status-double-enqueue-guard
verified: 2026-06-10T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: none
  previous_score: n/a
gaps: []
---

# Phase 34: Pipeline Queue-Depth Status & Double-Enqueue Guard — Verification Report

**Phase Goal:** Surface live SAQ queue depth on the pipeline dashboard so an in-flight analysis run is visible after a page refresh and the trigger buttons cannot double-enqueue.
**Verified:** 2026-06-10
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Refresh `/pipeline/` mid-run → persistent progress card with bar + "N queued · M active" | ✓ VERIFIED | `dashboard.html:16-17` renders `processing_card.html` on initial full-page load (no `oob` flag) AND `stats_bar.html:59-65` OOB-swaps the same `#processing-card` on each 5s poll. Card shows bar `style="width: {{ queue_progress_percent }}%"` + `{{ agent_queued }} queued · {{ agent_active }} active` when `agent_busy > 0` (`processing_card.html:27-31`). Initial render seeded server-side via `dashboard()` (`routers/pipeline.py:201-213`). |
| 2 | Run Analysis (+ agent-task buttons) disabled while `agent_busy>0` → cannot double-enqueue | ✓ VERIFIED | `stage_cards.html:29` Run Analysis `:disabled="...|| $store.pipeline.agentBusy > 0"`; Extract Metadata `:51`, Fingerprint `:73` same gate. `agent-busy-seed` re-pushed into store on every poll (`stats_bar.html:57`), so the gate refreshes live without re-rendering the button subtree. |
| 3 | `get_queue_activity` never raises (degrades to zero) — poll cannot 500 on Redis/app.state hiccup | ✓ VERIFIED | `services/pipeline.py:73-93` — two **independent** `try/except Exception` blocks (agent source L73-84, controller source L86-93). `AttributeError` (missing `app.state` in lifespan-skip tests) is a subclass of `Exception` → caught; each degrades only its own source to 0. Only `"queued"`/`"active"` kinds read (L78-79, L87-88); **never** references `"incomplete"`. Tests: `test_pipeline.py:155` (all-source degrade), `:172` (missing app.state / AttributeError path), `:182` (controller outage leaves agent intact). |
| 4 | All FOUR buttons render + correctly gated (agent_busy gates Analyze/Fingerprint/Extract-Metadata; controller_busy gates Proposals) | ✓ VERIFIED | `stage_cards.html`: Analyze (L23-32 → `/pipeline/analyze`), Extract Metadata (L45-54 → `/pipeline/extract-metadata`), Fingerprint (L67-76 → `/pipeline/fingerprint`), Generate Proposals (L89-98 → `/pipeline/proposals`). Three agent buttons gate on `agentBusy>0`; Proposals on `controllerBusy>0` (L95). New buttons hx-post to the EXISTING endpoints (`routers/pipeline.py:359,453`). |
| 5 | Full suite green, coverage ≥85%, ruff+mypy clean | ✓ VERIFIED | Phase test files: **58 passed** (`_queue_fakes_test`, `test_services/test_pipeline`, `test_routers/test_pipeline`, `test_processing_card_partial`, `test_stage_cards_partial`). Full suite: 1589 passed; the only 9 failed + 42 errors are 100% confined to the 4 Redis-dependent files (`test_agent_tracklists`, `test_execution_dispatch`, `test_agent_task_router`, `test_agent_exec_batches` — 961 connection-refused markers; environmental, no Redis in sandbox). Phase-module coverage: `services/pipeline.py` **100%**, `routers/pipeline.py` 88.54% (uncovered = pre-existing Phase 16 fingerprint API endpoints), TOTAL 90.52%. Ruff: "All checks passed!"; mypy: "Success: no issues found". |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `services/pipeline.py::get_queue_activity` | Per-source-isolated queue read, degrades to 0 | ✓ VERIFIED | L47-104; non-revoked-agent predicate `select(Agent).where(Agent.revoked_at.is_(None))` (L74), NOT `select_active_agent`. 100% covered. |
| `services/pipeline.py::queue_progress_percent` | DB-derived guarded percent | ✓ VERIFIED | L107-122; `round(analyzed/denom*100)` with `denom := analyzed+agent_busy`, returns 0 when idle. Pure helper, unit-tested. |
| `routers/pipeline.py::dashboard` | Seeds counts on first load | ✓ VERIFIED | L201-213 spreads `**activity` + `queue_progress_percent` into context. |
| `routers/pipeline.py::pipeline_stats_partial` | Seeds counts on poll | ✓ VERIFIED | L229-244 spreads `**activity` + `queue_progress_percent`, `oob_counts=True`. |
| `partials/processing_card.html` | Stable swap target, OOB gated to poll, idle-empty | ✓ VERIFIED | `#processing-card` outer (L20); `hx-swap-oob` only `{% if oob_counts %}`; empty when both idle (L21 guard). |
| `partials/stage_cards.html` | 4 gated buttons + store seeds | ✓ VERIFIED | 4 buttons; in-place `agent-busy-seed`/`controller-busy-seed` anchors (L12-13). |
| `partials/stats_bar.html` | OOB store-writes for busy counts + card | ✓ VERIFIED | L57-58 OOB busy seeds; L65 includes processing card inside `oob_counts` gate. |
| `base.html` Alpine store | `agentBusy`/`controllerBusy`/`metadataExtracted` defaults = 0 | ✓ VERIFIED | L97 `Alpine.store('pipeline', { discovered: 0, analyzed: 0, metadataExtracted: 0, agentBusy: 0, controllerBusy: 0 })`. |
| `tests/_queue_fakes.py::FakeQueue.count` | Wave-0 prerequisite | ✓ VERIFIED | L85-93 `async def count(self, kind)`, `set_counts`/`fail_count` helpers for degrade-path tests. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|----|--------|---------|
| `dashboard()` | `get_queue_activity` | direct await | ✓ WIRED | `routers/pipeline.py:201` |
| `pipeline_stats_partial()` | `get_queue_activity` | direct await | ✓ WIRED | `routers/pipeline.py:229` |
| `get_queue_activity` | per-agent queues | `app_state.task_router.queue_for(agent.id)` | ✓ WIRED | `services/pipeline.py:77` |
| `stats_bar.html` OOB seeds | `$store.pipeline.agentBusy/controllerBusy` | `x-init` on poll | ✓ WIRED | L57-58 |
| `stage_cards.html` buttons | store gates | `:disabled` bindings | ✓ WIRED | L29,51,73,95 |
| New Fingerprint/Extract buttons | existing endpoints | `hx-post` | ✓ WIRED | `/pipeline/fingerprint` (router L453), `/pipeline/extract-metadata` (router L359) |

### Anti-Patterns Found

None. No `TBD`/`FIXME`/`XXX`/`HACK`/`PLACEHOLDER` markers in any phase-modified file.

### Informational Observation (non-blocking)

`stats_bar.html` re-seeds `discovered`, `analyzed`, `agentBusy`, `controllerBusy` into the store on each 5s poll, but does **not** re-seed `metadataExtracted` (no `fingerprint-files-ready` OOB twin). Consequence: the Fingerprint button's `metadataExtracted===0` gate refreshes only on full-page load, not on poll. This does **not** affect the phase goal — the double-enqueue guard relies on `agentBusy>0`, which IS re-seeded every poll, and CONTEXT did not require live poll-refresh of the new Fingerprint ready-count. Noted for future polish only.

### Gaps Summary

No gaps. All five must-haves are delivered by the implemented code and confirmed by 58 passing phase tests plus direct file inspection. The `get_queue_activity` failure-isolation contract (independent per-source try/except, AttributeError + generic Exception caught, degrade-to-zero, no `"incomplete"` reference) is verified both in source (`services/pipeline.py:71-104`) and by dedicated tests. Both dashboard contexts seed the counts. The processing card has a stable swap target with poll-only OOB gating and an idle-empty state. All four buttons render with correct agent_busy/controller_busy gating against the existing endpoints. The Alpine store defines all required zero defaults. Ruff + mypy clean; phase-module coverage 90.52% (service helpers 100%). The only suite failures are environmental Redis-connection errors in 4 unrelated files, explicitly excluded from this phase's scope.

---

_Verified: 2026-06-10_
_Verifier: Claude (gsd-verifier)_
