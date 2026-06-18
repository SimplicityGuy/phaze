# Phase 44: Analyze Observability UI — Context

**Gathered:** 2026-06-18
**Status:** Ready for planning
**Source:** plan-phase inline triage (skip discuss-phase + UI-SPEC, per operator). Decisions resolved against the Phase 43 shipped backend + live codebase.

<domain>
## Phase Boundary

Surface the analysis outcomes that **Phase 43 already records** on the existing pipeline
dashboard and file/proposal views. Phase 43 shipped the backend truth (terminal
`ANALYSIS_FAILED` state, five `analysis` coverage columns, `sampled` flag, the worker-side
caps + terminal classification). Phase 44 is **read + one re-trigger** — no new analysis
algorithm, no schema beyond a payload field.

**In scope:**
1. Dashboard **straggler** count/list (long-running in-flight analyze jobs).
2. Dashboard **`ANALYSIS_FAILED`** count/list (terminal failures).
3. **"Sampled — more data available" badge** on files whose analysis was strided.
4. **"Deepen analysis" re-trigger** — re-enqueue a sampled file with an elevated/unbounded
   window cap via a new `ProcessFilePayload` flag, threaded through to `analyze_file`.
5. Regression tests for the new reads + the re-trigger.

**Out of scope (do NOT plan):**
- Distributed cloud burst analysis → **Phase 45 backlog** (gated on post-43 re-measure).
- Adding a `files.updated_at` / state-change timestamp column — **not needed**; in-flight
  truth comes from the `saq_jobs` table (see D-01). Do not add it.
- Backfilling the 79 analyzed-but-`DISCOVERED` rows or the 11k in-flight legacy jobs —
  operator/homelab redeploy concern, not phase code.
- Any new analysis math — caps/striding/coverage all shipped in Phase 43.
</domain>

<decisions>
## Implementation Decisions

### D-01 — "Straggler" = long-running in-flight analyze jobs (from `saq_jobs`, NOT `files`)
The `files` table has **no `updated_at` / state-change timestamp** (only `state` + the
`ix_files_state` index), so a literal "file stuck N hours" query is **not available** from
`files`. In-flight analysis is NOT a `FileState` either — a file sits in `FINGERPRINTED`
until the analysis PUT advances it to `ANALYZED`.

**Definition:** a straggler is an `analyze` (`process_file`) SAQ job with status `active`
that has been running longer than a threshold. Source of truth = the **`saq_jobs`** table,
which SAQ stamps with `started`/`touched`. Reuse the existing `_STAGE_BUSY_SQL` /
`get_stage_busy_counts` pattern in `services/pipeline.py` (deterministic-key prefix =
`process_file`, status `IN ('queued','active')`) and add an age predicate on the running
job. Threshold should be configurable (default tied to the inner analysis timeout — Phase 43
`analysis_inner_timeout_sec` = 6600s — or a dedicated `straggler_threshold_sec` knob). This
is the long-running-set signal from the 4h-timeout incident.

### D-02 — `ANALYSIS_FAILED` count/list from the indexed `files.state`
Terminal failures ARE a real `FileState` (`analysis_failed`, shipped Phase 43) and the
`files.state` column is indexed (`ix_files_state`). Count + list straight from
`files.state = 'analysis_failed'`. `ANALYSIS_FAILED` is currently **absent** from
`PIPELINE_STAGES` in `routers/pipeline.py` (intentionally deferred by 43-REVIEW IN-01) —
Phase 44 surfaces it. Straggler and failed are **two distinct buckets** (matching the
requirement "straggler/`ANALYSIS_FAILED` count + list"): straggler = still grinding,
failed = gave up.

### D-03 — Sampled badge driven by `analysis.sampled` (+ coverage counts for detail)
The badge renders when `analysis.sampled` is true. Use the four coverage counts
(`fine_windows_analyzed/total`, `coarse_windows_analyzed/total`) for a tooltip / detail line
(e.g. "fine 60/412 windows — sampled"). Render wherever per-file analysis is shown — the
existing `templates/proposals/partials/analysis_timeline.html` / proposals row detail is the
natural home; reuse the established badge partial idiom (e.g. `source_badge.html`,
`confidence_badge.html`). Pre-Phase-43 rows carry NULL coverage → treat NULL `sampled` as
"not sampled" (no badge), never as an error.

### D-04 — "Deepen analysis" = elevated-cap re-enqueue via a new payload flag
`ProcessFilePayload` (`schemas/agent_tasks.py`) currently has **no cap field** (only
`file_id`, `original_path`, `file_type`, `agent_id`, `models_path`). Add an optional cap
override (e.g. `fine_cap: int | None = None`, `coarse_cap: int | None = None`, default None →
worker uses its `AgentSettings` 60/30 defaults). The agent worker `process_file`
(`tasks/functions.py`) must pass the payload override into
`analyze_file(fine_cap=…, coarse_cap=…)` when present, else fall back to the
`AgentSettings` caps (the existing behavior). "Elevated/unbounded" = a large or `0`/sentinel
cap (recall `_stride_to_cap` treats `cap <= 0` as no-op = analyze ALL windows).

The control-plane action (button on the sampled-file view) POSTs to a new endpoint that
builds the elevated-cap payload and enqueues through **`enqueue_process_file`**
(`services/analysis_enqueue.py`) — extended to accept the cap overrides — so it carries the
FULL payload and the correct routing.

### D-05 — Enqueue routing + dedup discipline (incident guards — MANDATORY)
The deepen re-enqueue MUST go through the per-agent analysis queue via the existing
`enqueue_process_file` funnel (NOT the consumer-less default queue — default-queue
misrouting incident, Phase 30) and MUST build the complete `ProcessFilePayload` (v4.0.8
payload incident dead-lettered jobs that carried only `file_id`). Note the deterministic key
`process_file:<file_id>`: a re-trigger of an already-`ANALYZED` file (no in-flight job) is a
fresh enqueue, but if a job is still in-flight the key dedups it to a no-op — the plan must
state the expected behavior for re-deepening.

### D-06 — Dashboard reads are degrade-safe (never-500 the 5s poll)
All new dashboard counts/lists feed the hot 5s `/pipeline/stats` poll. Follow the
established discipline: wrap new `saq_jobs`/`files` reads in `session.begin_nested()`
SAVEPOINTs, log-and-return-zero/empty on any DB error, never raise into the poll
(mirrors `get_stage_busy_counts` / `get_search_busy_count`).

### Claude's Discretion
- Exact threshold knob name/default for D-01 (suggest `straggler_threshold_sec`).
- Whether the straggler/failed lists are inline on the dashboard or an HTMX-expanded
  partial/drill-down (follow the existing dashboard card + partial idiom).
- Payload field shape for D-04 (two `*_cap` fields vs a single `deepen`/multiplier flag) —
  pick the one that threads most cleanly into `analyze_file`'s existing `fine_cap`/`coarse_cap`
  kwargs.
- Button placement/affordance for the deepen action within the sampled-file view.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase 43 backend being surfaced (read these summaries)
- `.planning/phases/43-analyze-throughput-fix-bound-per-file-analysis-cost-kill-on-/43-02-SUMMARY.md` — five-field coverage contract + `sampled` semantics; `_stride_to_cap` (cap<=0 = analyze all).
- `.planning/phases/43-analyze-throughput-fix-bound-per-file-analysis-cost-kill-on-/43-03-SUMMARY.md` — `ANALYSIS_FAILED` enum, coverage columns (rev 021), state machine.
- `.planning/phases/43-analyze-throughput-fix-bound-per-file-analysis-cost-kill-on-/43-04-SUMMARY.md` — worker `process_file` caps threading + coverage forwarding.

### Code to read/modify
- `src/phaze/routers/pipeline.py` — pipeline dashboard route; `PIPELINE_STAGES`; where counts render.
- `src/phaze/services/pipeline.py` — `get_stage_busy_counts` + `_STAGE_BUSY_SQL` (saq_jobs in-flight pattern to extend for stragglers); SAVEPOINT degrade idiom.
- `src/phaze/templates/pipeline/dashboard.html` — dashboard surface (Phase 34/35 cards live here).
- `src/phaze/models/file.py` — `FileState.ANALYSIS_FAILED`; `ix_files_state`; NOTE no `updated_at`.
- `src/phaze/models/analysis.py` — the five coverage columns + `sampled`.
- `src/phaze/schemas/agent_tasks.py` — `ProcessFilePayload` (add cap override here).
- `src/phaze/services/analysis_enqueue.py` — `enqueue_process_file` funnel (extend for deepen; routing + dedup).
- `src/phaze/tasks/functions.py` — agent `process_file`; thread payload cap override into `analyze_file`.
- `src/phaze/services/analysis.py` — `analyze_file(fine_cap, coarse_cap)` already accepts overrides.
- `src/phaze/templates/proposals/partials/analysis_timeline.html` + sibling badge partials — sampled badge home.
</canonical_refs>

<specifics>
## Specific Ideas

- Reuse `get_stage_busy_counts`'s `saq_jobs` scan + SAVEPOINT pattern for the straggler count;
  add a running-age predicate rather than inventing a new data source.
- `_stride_to_cap(cap <= 0) → no-op (analyze ALL windows)` is the "unbounded deepen" lever —
  the deepen payload can send a sentinel cap to request a full re-analysis.
- Treat NULL coverage (`sampled` NULL on pre-43 rows) as "not sampled".
</specifics>

<deferred>
## Deferred Ideas

- Distributed cloud burst analysis → Phase 45 backlog (gated; may be moot after the Phase 43
  redeploy + re-measure).
- Per-file state-change timestamp on `files` — not pursued; SAQ job timestamps cover the
  straggler signal.
</deferred>

---

*Phase: 44-analyze-observability-ui-straggler-failed-count-sampled-badg*
*Context gathered: 2026-06-18 via plan-phase inline triage*
