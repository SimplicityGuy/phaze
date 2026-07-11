# Phase 87: Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority - Research

**Researched:** 2026-07-10
**Domain:** Server-rendered operator console (FastAPI + Jinja2/HTMX) over a derived per-stage status layer; a new distinct `skipped` marker in the single-source derivation contract
**Confidence:** HIGH (all findings are file:line-verified against the shipped codebase; zero new dependencies)

## Summary

This phase is 80% presentational re-wiring of already-live backend surfaces and 20% one genuinely
new, correctness-critical backend slice: the distinct `skipped` marker (CONTEXT D-13). Every UI-side
requirement (UI-01 pill matrix, UI-02 failure retry, UI-03 eligibility trace, UI-05 orphan badge,
PRIO-01 priority stepper) reuses an endpoint, service helper, or template partial that **already
exists and is tested** — the retry endpoints (`routers/pipeline.py:934,1017`), the priority/pause/resume
endpoints (`routers/pipeline_stages.py`, fully live but orphaned from the v7.0 UI), the pending/failed
service helpers, the `_file_table.html` scaffold, and the `scan_status_pill.html` token. No new
framework, library, or dependency is required or permitted.

The one non-trivial slice is the `skipped` marker. The load-bearing finding: the three enrich stages
do **not** share a uniform 1:1 output table (metadata and analysis are 1:1; **fingerprint is 1:N**
via `fingerprint_results`), so the Phase-81 "add a `failed_at` column to the 1:1 table" shape does
**not** generalize. The correct shape is a **small sidecar table keyed on `(file_id, stage)`** — modeled
on the `dedup_resolution` sidecar (migration 032) — carrying the D-09 reason + a timestamp, with a
`UNIQUE(file_id, stage)` constraint giving the ≤1-row-per-(file,stage) invariant. The marker composes
into the derivation via **one new `skipped_clause(stage)` builder** threaded into `stage_status_case`
(a 5th CASE branch), `eligible_clause` (a `~skipped` conjunct), and `domain_completed_clause` (skipped ⇒
domain-complete). Because the pending sets and recovery already read *exclusively* through
`eligible_clause` / `domain_completed_clause`, this single-source thread makes a skipped file leave all
three enrich pending sets AND stay un-re-enqueued by recovery **for free** — no per-caller edits.

**Numbers-will-look-different note (carry to SUMMARY, per D-specifics):** as `failed` and the new
`skipped` bucket become independently visible and per-stage eligibility renders simultaneously, counts
shift versus the old serially-gated view. This is the fix, not a regression (mirrors the `get_fingerprint_progress`
D-11 note at `fingerprint.py:256`).

**Primary recommendation:** Land the `skipped` marker as a `(file_id, stage)` sidecar table (migration 037),
add ONE `skipped_clause` builder threaded into the three existing derivation composers with a 5-member
`Status` enum, extend the DERIV-04 equivalence harness with skipped cells on all three axes, and confirm
the writer is **purely additive** (never clears `failed_at`) so the Phase-79 shadow-compare gate stays
green without allowlisting. Everything else is UI re-wiring of live endpoints and partials.

## User Constraints (from CONTEXT.md)

### Locked Decisions (do not re-litigate — research HOW, not WHETHER)
- **D-00a:** `stage_status_case(stage)` is the single 4-bucket CASE; pill matrix, failure filter, and
  four-bucket counts all read the SAME definition. No second status-derivation path.
- **D-00b:** Failed *analyze* is terminal — manual retry only (the 44.5K over-enqueue guard,
  `ELIGIBLE_AFTER_FAILURE[ANALYZE]=False`). Failed metadata/fingerprint auto-retry. Retry UI offers NO
  auto-retry path for analyze.
- **D-00c:** Never a whole-corpus scan per poll; files table paginated (keyset/offset); every derived
  read `_safe_count`/SAVEPOINT-degrade-safe (never 500 the 5s poll).
- **D-01:** Matrix form = a row of labeled pills (6 pills: Meta / FP / Analyze / Prop / Appr / Exec).
- **D-02:** Home = BOTH a paginated files table AND the right pane. Reuse `_file_table.html`.
- **D-03:** Failed files surface as a status filter on the files table (not a separate page).
- **D-04:** Retry granularity = BOTH per-file and bulk-per-stage. Analyze bulk-retry respects the
  terminal-analyze guard (manual retry, not auto-loop).
- **D-05:** Orphaned/stuck-work count = a DAG-rail badge near the affected stage, derived from the ledger.
- **D-06:** Trace trigger = per-stage, in the right pane (click a stage pill).
- **D-07:** Trace depth = named conjuncts (`done? · in-flight? · upstream met? · terminal fail?`) + the
  specific blocker, named from `ELIGIBILITY_DAG`.
- **D-08:** Force-done/skip writes a **distinct `skipped` marker** (sidecar, analogous to the Phase-81
  failure markers) — derivation treats it as stage-satisfied for eligibility + downstream unblocking,
  but reports a **distinct `skipped` pill**, NOT counterfeit `done`. Honesty over convenience.
- **D-09:** Guard = confirm dialog + **required** free-text reason recorded with the marker. Per-file only.
- **D-10:** Scope = enrich stages ONLY (metadata / fingerprint / analyze). Propose/approve/execute must
  NOT be force-skippable (approval-bypass hazard).
- **D-11:** Re-wire BOTH the priority stepper AND pause/resume, per-stage on the DAG rail, to the live
  `POST /pipeline/stages/{stage}/{priority,pause,resume}` endpoints. Add clarifying label/tooltip.
  Response `{stage, priority, paused}` from the durable control row.
- **D-13:** Phase 87 is NOT purely presentational: (a) schema + Alembic migration for the skip marker
  (mirror ≤1-row invariant; sync migration; mirrored `downgrade()`; integration test in
  `tests/integration/test_migrations/`; never reference `saq_jobs`); (b) a writer behind the force-skip
  endpoint stamping marker + reason; (c) a derivation read so `stage_status_case`/`eligible_clause`
  surface `skipped` as stage-satisfied AND its own bucket — drift-locked via the Phase-78 DERIV-04
  equivalence harness (extend, don't bypass); (d) the Phase-79 shadow-compare gate stays green.

### Claude's Discretion
- Files-table default scope + filter set + pagination style (keyset vs offset) — constrained by D-00c.
- Pill labels & bucket color tokens (reuse existing tokens); the `skipped` pill's distinct visual
  treatment. **NOTE: the 87-UI-SPEC.md already fixes these** (violet + `⊘` glyph + dashed ring); treat
  the UI-SPEC as the locked visual contract.
- Right-pane layout composition.
- Retry response shape — reuse `metadata_retry_response.html` / `retry_failed_response.html` vs new.
- Plan/PR decomposition — natural seams (a) skipped-marker slice; (b) files table + matrix + filters;
  (c) right-pane trace + force-skip; (d) orphan badge + priority re-wire. Small blast-radius per PR.

### Deferred Ideas (OUT OF SCOPE)
- Lane / agent drill-in views → Phase 88 (DRILL-01..03).
- `files.state` column drop + `FileState` enum deletion + remaining `.state=` writers → Phase 90.
- DENORM-01 (denormalized stored stage-bitmap column) → only if a poll-time measurement proves the
  derived files-table query too slow. YAGNI.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| UI-01 | Per-file derived stage matrix (pills) in a paginated files table + expanded right pane; retire raw-enum "State" | `stage_status_case` (stage_status.py:364) drives pills; `_file_table.html` scaffold; retire sites `metadata_workspace.html:40,50`, `analyze_workspace.html:81-86`. Per-page derivation via correlated `stage_status_case` in a paginated `SELECT ... LIMIT/OFFSET` — no whole-corpus scan. |
| UI-02 | Failed files as status filter; per-file + bulk-per-stage retry | Live bulk endpoints `retry_analysis_failed` (pipeline.py:934), `retry_metadata_failed` (pipeline.py:1017); failed-file helpers `get_analysis_failed_files` (pipeline.py:1113), `get_metadata_failed_files` (pipeline.py:1461); fingerprint failures self-retry via `eligible_clause(FINGERPRINT)` pending set (no manual endpoint needed). |
| UI-03 | Per-stage eligibility trace in right pane (named conjuncts + blocker) | Pure `eligible()` + `ELIGIBILITY_DAG` (stage.py:61,215); single-row `resolve_status` per stage — cheap, no SQL scan. Render conjuncts from `resolve_status` outputs + `ELIGIBILITY_DAG[stage]` upstream names. |
| UI-04 | Force-done/skip control (enrich only) writing distinct `skipped` marker; confirm + reason | NEW: sidecar table + `skipped_clause` builder + writer endpoint. See "The `skipped` marker" section. |
| UI-05 | Orphaned/stuck-work count as DAG-rail badge | Derive from `scheduling_ledger` (in_flight authority, 78 D-01) minus live `saq_jobs` keys minus `domain_completed`. `get_live_job_keys` (pipeline.py:566) + `_safe_count`. See Orphan section + the naive-`enqueued_at` footgun. |
| PRIO-01 | Re-wire priority stepper + pause/resume to live endpoints on DAG rail | `routers/pipeline_stages.py` (fully live); DAG-rail overlay `routers/pipeline.py:219-227` seeds `{stage}Paused`/`{stage}Priority`. Pure UI re-wire; response `{stage, priority, paused}`. |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Per-stage status derivation | API/Backend (`services/stage_status.py` SQL twin + `enums/stage.py` Python twin) | — | Single-source predicate layer; both twins drift-locked by DERIV-04 harness |
| `skipped` marker persistence | Database (new sidecar table) | API (writer endpoint) | Derive-don't-store honored; marker is the sole stored fact, status stays derived |
| Pill matrix render | Frontend Server (Jinja2 partials) | — | Server-rendered; reads `stage_status_case` results into pill tokens |
| Paginated files table | API (paginated query) + Frontend Server (`_file_table.html`) | — | Keyset/offset LIMIT; correlated `stage_status_case` per page row (never whole-corpus) |
| Eligibility trace | API (single-row `resolve_status`) + Frontend Server | — | Cheap per-file Python conjunct eval; NOT a SQL scan |
| Failure retry | API (live bulk endpoints + new per-file scope) | Task queue (SAQ enqueue) | Reuses guarded funnel + deterministic dedup key |
| Priority/pause/resume | API (`pipeline_stages.py`, live) + Frontend Server (rail) | Task queue (backlog reorder) | Pure UI re-wire to durable control row |
| Orphan count | API (ledger − live keys − domain_completed) + Frontend Server (rail badge) | — | Degrade-safe count; rides existing `#pipeline-stats` OOB fanout |

## Standard Stack

**Zero new dependencies (hard constraint).** Everything below is already installed and in use.

### Core (all pre-existing)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| FastAPI | (installed) | Router endpoints | Existing `/pipeline/*`, `/record/*` routers `[VERIFIED: codebase]` |
| SQLAlchemy 2.0 async | (installed) | `ColumnElement` clause builders, correlated `exists()` | The entire `stage_status.py` house style `[VERIFIED: services/stage_status.py]` |
| asyncpg | (installed) | Async PG driver | Existing `[VERIFIED: CLAUDE.md stack]` |
| Alembic | (installed) | Migration 037 for the sidecar table | Migrations 032-036 precedent `[VERIFIED: alembic/versions/]` |
| Jinja2 + HTMX 2.0.10 + Tailwind v4 + Alpine.js 3.15.12 | (installed) | Server-rendered partials | v7.0 DAG shell `[CITED: 87-UI-SPEC.md]` |

### Supporting (all pre-existing helpers to reuse — do NOT rebuild)
| Helper | Location | Reuse For |
|--------|----------|-----------|
| `stage_status_case(stage)` | `services/stage_status.py:364` | Pill matrix bucket (add 5th `skipped` branch) |
| `done_clause` / `failed_clause` / `inflight_clause` / `eligible_clause` / `domain_completed_clause` | `services/stage_status.py` | Derivation composition targets for `skipped_clause` |
| `resolve_status` / `eligible` / `domain_completed` / `ELIGIBILITY_DAG` | `enums/stage.py` | Python twins + the D-07 trace conjuncts |
| `get_analysis_failed_files` / `get_metadata_failed_files` | `services/pipeline.py:1113,1461` | UI-02 failed lists (already `failed_clause`-derived) |
| `get_metadata_pending_files` / `get_fingerprint_pending_files` / `get_discovered_files_with_duration` | `services/pipeline.py:1441,1480,1151` | The 3 enrich pending sets (all read `eligible_clause`) |
| `retry_analysis_failed` / `retry_metadata_failed` | `routers/pipeline.py:934,1017` | UI-02 bulk retry (add a per-file scoped variant) |
| `set_priority` / `pause` / `resume` | `routers/pipeline_stages.py:82,103,117` | PRIO-01 (live; UI re-wire only) |
| `get_live_job_keys` | `services/pipeline.py:566` | UI-05 orphan derivation (ledger − live) |
| `_safe_count` / `begin_nested()` SAVEPOINT | `services/pipeline.py:303` | Every new derived read (degrade-safe) |
| `_file_table.html` / `scan_status_pill.html` / `metadata_retry_response.html` / `retry_failed_response.html` | `templates/pipeline/partials/` | UI-01/UI-02 scaffolds |
| `tracklists/partials/pagination.html` (+ 3 siblings) | `templates/*/partials/pagination.html` | Files-table pagination controls |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| Sidecar `(file_id, stage)` skip table | `skipped_at` column on 1:1 tables (Phase-81 shape) | **REJECTED: fingerprint has no 1:1 table** (`fingerprint_results` is 1:N). Column shape can't cover fingerprint uniformly. Sidecar is the only uniform enrich-wide shape. |
| Skip marker as new CASE branch | Folding skip into `done_clause` | REJECTED by D-08: folding into `done_clause` makes the pill read `done` (counterfeit). Must be a separate clause + separate CASE branch. |

**Installation:** None. `uv sync` unchanged.

**Version verification:** N/A — zero new packages. All imports resolve against the current lockfile.

## Package Legitimacy Audit

**Not applicable — this phase installs ZERO external packages** (hard constraint: no new deps). No
registry lookups, no slopcheck run required. All code reuses first-party modules already in `src/phaze/`.

## The `skipped` marker — mechanics (the D-13 non-UI slice)

This is the sharpest correctness surface. Findings below are all `[VERIFIED: codebase]`.

### 1. Shape: a `(file_id, stage)` sidecar table (NOT a per-table column)

The Phase-81 failure markers are **columns on the 1:1 output tables**:
`analysis.failed_at` / `analysis.error_message` (`models/analysis.py`, migration 032) and
`metadata.failed_at` / `metadata.error_message` (`models/metadata.py`, migration 032). Phase 81's
migration 033 added a `CHECK NOT (analysis_completed_at IS NOT NULL AND failed_at IS NOT NULL)` XOR
on `analysis`.

**Key landmine:** this column shape works for metadata and analyze (both 1:1) but **fingerprint has NO
1:1 table** — `done_clause(FINGERPRINT)` / `failed_clause(FINGERPRINT)` aggregate over the 1:N
`fingerprint_results` table (`models/fingerprint.py`, no aggregate row). There is no single row on which
to hang a `skipped_at` column for fingerprint. Therefore the marker MUST be a sidecar.

**Recommended shape** (mirrors the `dedup_resolution` sidecar created in migration 032:130-142):

```
CREATE TABLE stage_skip (
    id             UUID PRIMARY KEY,
    file_id        UUID NOT NULL REFERENCES files(id),
    stage          VARCHAR NOT NULL,            -- 'metadata' | 'analyze' | 'fingerprint' (D-10 enrich only)
    reason         TEXT NOT NULL,               -- D-09 required free-text reason
    skipped_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMP NOT NULL DEFAULT now(),
    updated_at     TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT uq_stage_skip_file_stage UNIQUE (file_id, stage)   -- the <=1-row-per-(file,stage) invariant
);
```

The `UNIQUE(file_id, stage)` gives the ≤1-row invariant that mirrors the Phase-81 markers, and doubles
as the covering index for the correlated `exists()` probe. Consider whether to constrain `stage` to the
three enrich values via a CHECK (belt-and-suspenders on D-10) or leave it to the writer.

**Migration:** next revision is **037** (`down_revision="036"`). Follow the 032/033 template exactly:
sync `upgrade()`/`downgrade()`, mirrored `downgrade()` (`op.drop_table("stage_skip")`), bare
constraint names (the `pk_`/`uq_`/`fk_` naming convention re-applies prefixes — see the 032:66-67 and
033:55-57 "double-prefix" warnings), and mirror the table into the ORM `__table_args__` for the
empty-autogenerate-diff contract. **CRITICAL: never reference `saq_jobs`** (020/031/032/033 banner).
Integration test in `tests/integration/test_migrations/test_037_*.py` (follow
`test_migration_032_additive_schema.py` / `test_migration_033_additive_check.py`).

### 2. Composition into the derivation layer (single-source thread)

Add ONE new builder `skipped_clause(stage: Stage) -> ColumnElement[bool]` in `services/stage_status.py`,
enrich-only (guard `stage not in ELIGIBLE_AFTER_FAILURE` → `ValueError`, same shape as `eligible_clause`):

```python
def skipped_clause(stage: Stage) -> ColumnElement[bool]:
    return exists(select(StageSkip.id).where(StageSkip.file_id == FileRecord.id, StageSkip.stage == stage.value))
```

Then thread it into **three** existing composers (and their Python twins):

**(a) `stage_status_case` — a 5th CASE branch, precedence `in_flight ≻ done ≻ skipped ≻ failed ≻ not_started`:**
```python
return case(
    (inflight_clause(stage), Status.IN_FLIGHT.value),
    (done_clause(stage),     Status.DONE.value),
    (skipped_clause(stage),  Status.SKIPPED.value),   # NEW — after done, BEFORE failed
    (failed_clause(stage),   Status.FAILED.value),
    else_=Status.NOT_STARTED.value,
)
```
`skipped ≻ failed` is **load-bearing**: the primary UI-04 target is a *terminally-failed* analyze (a row
carrying `failed_at`). After skipping, that file must read the distinct `skipped` pill, not `failed`. Because
the skip writer is purely additive (does NOT clear `failed_at`), `failed_clause` still returns True — so
the CASE order is what makes `skipped` win. `done ≻ skipped` keeps genuine completion honest if both ever
coexist. **Only `stage_status_case` (enrich stages) gains the branch** — the downstream stages have no
skip marker (D-10).

**(b) `eligible_clause` — a `~skipped` conjunct** so a skipped file leaves the pending set:
```python
conjuncts = [not_(inflight_clause(stage)), not_(done_clause(stage)), not_(skipped_clause(stage))]  # NEW ~skipped
if not ELIGIBLE_AFTER_FAILURE[stage]:  # analyze terminal carve-out (unchanged)
    conjuncts.append(not_(failed_clause(stage)))
```
This is the elegance: `get_metadata_pending_files` (pipeline.py:1441), `get_fingerprint_pending_files`
(pipeline.py:1480), and `get_discovered_files_with_duration` (pipeline.py:1151) ALL read exclusively
through `eligible_clause` — so a skipped file leaves all three pending sets with **zero per-caller edits**.

**(c) `domain_completed_clause` — skipped ⇒ domain-complete** so recovery never re-enqueues:
```python
disjuncts = [done_clause(stage), skipped_clause(stage)]      # skipped is always domain-complete
if FAILURE_IS_TERMINAL[stage]:
    disjuncts.append(failed_clause(stage))
return or_(*disjuncts)
```
`reenqueue.py` reads exclusively through `domain_completed_clause` (`_build_done_sets`, `_select_done_analyze_ids`),
so a skipped file is treated as "don't re-run" by both automatic and manual recovery with zero edits.
This closes the Phase-42 "UI/API/recovery must not drift" precedent the CONTEXT flags.

**Python twins (`enums/stage.py`):** add `Status.SKIPPED = "skipped"` (5th member); teach `resolve_status`
to take a `skipped: bool` scalar and return `Status.SKIPPED` after done/before failed; add `~skipped`
to the `eligible` enrich branch; add skipped-is-complete to `domain_completed`. The DB-free import
boundary (stdlib-only, T-78-01) is preserved — a bool scalar owned by the caller, no model import.

### 3. Downstream unblocking — THE sharpest open question (see Open Questions OQ-1)

D-08 says skipped is "stage-satisfied for eligibility + downstream unblocking." Eligibility (a) and
recovery (c) above are covered. **Downstream unblocking is NOT automatic** and needs an explicit decision:
`get_proposal_pending_batches` (pipeline.py:1520) — the propose convergence reader — does NOT read through
`done_clause(ANALYZE)`. It reads `FileRecord.state.in_([ANALYZED, METADATA_EXTRACTED])` AND
`analysis_completed_at IS NOT NULL` directly. A skipped analyze has `completed_at` NULL and (likely)
`state='analysis_failed'`, so it would NOT flow into propose. See OQ-1 for options.

### 4. Shadow-compare stays green WITHOUT allowlisting (verified reasoning)

`run_shadow_compare` (`services/shadow_compare.py`) asserts `state = X ⇒ <derived>` using `done_clause`/
`failed_clause`/raw `exists`, and **deliberately never `stage_status_case`** (shadow_compare.py:16-19).
A force-skip is derive-don't-store: it writes ONLY the sidecar and adds NO new `FileState` value, so it
adds no `INVARIANTS` entry. The critical check: does skipping a file false-flag an EXISTING invariant?
The `analysis_failed` invariant asserts `state='analysis_failed' ⇒ failed_clause(ANALYZE)`. Since the
skip writer is **purely additive (never clears `analysis.failed_at`)**, `failed_clause(ANALYZE)` still
returns True → the implication still holds → **no false flag, no allowlist needed.** This makes the
"writer is purely additive" property a hard requirement, not a nicety — call it out in the plan and test it.

## Architecture Patterns

### System Architecture Diagram (data flow)

```
                    ┌─────────────────────────────────────────────────────────┐
   operator ──────► │  Files table (paginated)          Right pane (record/{id})│
   (browser)        │  ├ path + 6-pill matrix           ├ expanded 6-pill matrix│
                    │  ├ status filter (URL query)       ├ click pill → trace    │
                    │  └ per-row Retry (failed cells)    └ Force-skip (enrich)    │
                    │  DAG rail: orphan badge + priority/pause steppers          │
                    └───────────┬──────────────────────────────┬────────────────┘
                                │ HTMX GET/POST                 │ single #pipeline-stats poll (5s, OOB)
                                ▼                               ▼
        ┌───────────────────────────────────────────────────────────────────────┐
        │ FastAPI routers:  /files (NEW paginated)  /record/{id}  /pipeline/*     │
        │   /pipeline/analysis-failed/retry (live)  /pipeline/metadata-failed/... │
        │   /pipeline/stages/{stage}/{priority,pause,resume} (live)               │
        │   POST force-skip (NEW)  → writer stamps stage_skip(file_id,stage,reason)│
        └───────────┬──────────────────────────────────────────┬─────────────────┘
                    │ reads                                      │ writes marker
                    ▼                                            ▼
        ┌───────────────────────────────────┐        ┌──────────────────────────┐
        │ SINGLE-SOURCE derivation layer     │        │  stage_skip (sidecar)    │
        │  stage_status_case (5-way CASE)    │◄───────┤  (file_id, stage, reason)│
        │  eligible_clause  (+ ~skipped)     │        └──────────────────────────┘
        │  domain_completed_clause (+skipped)│
        │  DERIV-04 harness locks SQL≡Python │
        └───────────┬───────────────────────┘
                    │ consumed by (zero-edit)
                    ▼
        ┌───────────────────────────────────────────────────────────┐
        │ pending sets (3 enrich) · recovery/reenqueue · shadow-compare│
        └───────────────────────────────────────────────────────────┘
```

### The 6-pill ↔ 7-stage mapping (LANDMINE)

`Stage` has **7** members (`enums/stage.py:33`): `metadata, analyze, fingerprint, tracklist, propose,
review, apply`. The matrix shows **6** pills: **Meta=metadata · FP=fingerprint · Analyze=analyze ·
Prop=propose · Appr=review · Exec=apply**. `tracklist` is NOT in the matrix. Note the label remaps:
**Appr → `Stage.REVIEW`** and **Exec → `Stage.APPLY`**. Getting these backwards silently mislabels pills.

### Pattern: paginated per-page derivation (no whole-corpus scan, D-00c)

Build the files-table query as `select(FileRecord, stage_status_case(s1), stage_status_case(s2), ...)`
with `.order_by(FileRecord.id).limit(N).offset(M)` (or keyset on `id`). The `stage_status_case`
correlated subqueries evaluate **only for the N rows on the page** (they correlate to `FileRecord`), not
the corpus. Wrap the count for the pager (if using offset) in `_safe_count`. The partial indexes
(`ix_metadata_failed`, `ix_analysis_completed`, `ix_analysis_failed`, `ix_fprint_success`) back the
`done`/`failed` probes. Keyset pagination avoids the `COUNT(*)` whole-corpus scan entirely (recommended
per D-00c; discretion left to planner).

### Pattern: single-row eligibility trace (D-07, cheap)

For the trace, load one file's scalars per stage (the `load_scalars` shape at
`test_stage_status_equivalence.py:351`) and call `resolve_status` + evaluate the `eligible()` conjuncts in
Python. Render: `done?` (status==DONE), `in-flight?` (status==IN_FLIGHT), `upstream met?`
(`all(status_map[u]==DONE for u in ELIGIBILITY_DAG[stage])` — **for enrich stages this is vacuously true**
since enrich upstreams are empty), `terminal fail?` (status==FAILED and not ELIGIBLE_AFTER_FAILURE[stage]).
Name the blocker from `ELIGIBILITY_DAG[stage]`. This is a single-row read, NOT a scan.

**Trace subtlety:** with `skipped` added, decide how the trace renders a skipped upstream — `upstream met?`
should treat a skipped upstream as met (satisfied), tying back to OQ-1's `stage_satisfied = done OR skipped`.

### Anti-Patterns to Avoid
- **Second status path:** never derive a bucket outside `stage_status_case` (D-00a). The pill, the
  filter, and the counts must all read the same CASE.
- **Folding skip into `done_clause`:** counterfeits `done` (D-08). Keep `skipped_clause` separate.
- **Clearing `failed_at` on skip:** breaks the shadow-compare `analysis_failed` invariant AND loses the
  audit fact. The writer is additive-only.
- **Whole-corpus `COUNT(*)` per poll:** violates D-00c/PERF-01. Prefer keyset pagination.
- **Bypassing the DERIV-04 harness:** recreates the exact drift class the milestone exists to prevent.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Per-stage status bucket | A new CASE / status enum path | Extend `stage_status_case` (5th branch) | D-00a single-source; anything else drifts |
| "Skipped leaves pending set" | Per-caller `.where(~skip...)` in each of 3 pending helpers | ONE `~skipped_clause` conjunct in `eligible_clause` | All 3 pending sets + manual triggers read `eligible_clause` |
| "Recovery skips skipped files" | A new recovery filter | skipped disjunct in `domain_completed_clause` | `reenqueue.py` reads only `domain_completed_clause` |
| Failed-file lists | New queries | `get_analysis_failed_files` / `get_metadata_failed_files` | Already `failed_clause`-derived, tested |
| Bulk retry funnel | New enqueue path | `retry_analysis_failed` / `retry_metadata_failed` | Guarded (NoActiveAgentError), deterministic dedup key, Phase-30 hardened |
| Priority/pause/resume backend | New endpoints | `routers/pipeline_stages.py` (live) | Fully implemented + threat-modeled; UI re-wire only |
| Degrade-safe counts | Raw `session.execute` | `_safe_count` / `begin_nested()` | Never-500 5s-poll discipline |
| Pagination controls | New template | `tracklists/partials/pagination.html` | Existing precedent |
| Pill token | New markup | `scan_status_pill.html` geometry (`text-xs font-semibold px-2 py-0.5 rounded-full`) | Project-wide pill convention (70 uses) |

**Key insight:** the derivation layer is deliberately single-source. Threading `skipped_clause` into
`eligible_clause` + `domain_completed_clause` propagates to every pending set, every manual trigger, and
both recovery paths with no per-caller code. Hand-rolling per-caller skip filters is how drift returns.

## Runtime State Inventory

This phase ADDS a marker; it is not a rename. But it introduces new stored state and changes derived
reads, so the runtime-state discipline still applies:

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | NEW `stage_skip` sidecar table (migration 037). No existing data carries a skip concept — **greenfield marker, no backfill** (unlike 032's failure-marker backfill; there is no historical "skipped" source). | Create table; NO backfill statement. |
| Live service config | None — no external service (n8n/Datadog/etc.) references stage status. Verified: skip is a DB-only concept. | None. |
| OS-registered state | None. | None — verified (no scheduler/pm2 involvement in this phase). |
| Secrets/env vars | None. | None. |
| Build artifacts | New ORM model `StageSkip` must be imported in `models/__init__.py` and in the Alembic `env.py` target metadata for autogenerate to see it (empty-diff contract). | Register the model. |
| Derived-read drift | `eligible_clause` + `domain_completed_clause` + `stage_status_case` change → propagates to 3 pending sets, both recovery paths, and shadow-compare. | Covered by single-source thread + DERIV-04 harness extension; NO per-caller edits. |

## Common Pitfalls

### Pitfall 1: fingerprint has no 1:1 table
**What goes wrong:** copying the Phase-81 "add a `skipped_at` column" shape onto fingerprint.
**Why:** `fingerprint_results` is 1:N; there's no aggregate row.
**How to avoid:** use the `(file_id, stage)` sidecar table (uniform across all 3 enrich stages).
**Warning sign:** a plan that adds columns to `analysis`/`metadata` but has no home for fingerprint's skip.

### Pitfall 2: skip must WIN over failed in the CASE, but NOT over done
**What goes wrong:** placing the `skipped` branch in the wrong CASE position → a skipped terminally-failed
analyze still renders `failed`, or a genuinely-completed stage renders `skipped`.
**How to avoid:** precedence `in_flight ≻ done ≻ skipped ≻ failed ≻ not_started`. Add a DERIV-04 cell that
seeds `analysis.failed_at` + a skip marker and asserts `skipped`.

### Pitfall 3: clearing `failed_at` on skip breaks shadow-compare
**What goes wrong:** the writer "tidies up" by nulling `analysis.failed_at`.
**Why:** the `analysis_failed` shadow invariant asserts `state='analysis_failed' ⇒ failed_clause(ANALYZE)`;
nulling `failed_at` makes it diverge, AND destroys the audit fact.
**How to avoid:** writer is additive-only (sidecar row + reason). Test the shadow gate stays green post-skip.

### Pitfall 4: naive `enqueued_at` in the orphan count (from project memory)
**What goes wrong:** `scheduling_ledger.enqueued_at` is a **naive** `TIMESTAMP` (no tz — `models/scheduling_ledger.py:63`).
Comparing it against a timezone-aware `datetime.now(UTC)` raises `TypeError` and aborts the transaction.
**How to avoid:** if the orphan/stuck derivation uses a staleness threshold, compare naive-to-naive (or
cast), and wrap in `_safe_count`. This is the documented "ledger enqueued_at naive-timestamp footgun."

### Pitfall 5: bare `status IN (...)` reserializes to `= ANY(ARRAY[...])`
**What goes wrong:** breaks the empty-autogenerate-diff contract for any partial index on the new table.
**How to avoid:** if the skip table gets a partial index, spell predicates exactly as Postgres renders them
(see `ix_fprint_success` at `models/fingerprint.py` + migration 032:156). A plain `UNIQUE(file_id, stage)`
b-tree avoids this entirely — recommended.

### Pitfall 6: mutation-test the new derivation guards (Phase 84 standing rule, project memory)
**What goes wrong:** a GREEN skip test proves nothing (e.g., a line-grep guard blind to multi-line
SQLAlchemy, or an AST blind to `.values(**splat)`).
**How to avoid:** for each new DERIV-04 skip cell and the "skipped leaves pending set" / "recovery skips
skipped" tests, break the source (drop the `~skipped` conjunct), watch RED, restore. Check false positives.

### Pitfall 7: get_session never auto-commits
**What goes wrong:** the force-skip writer flushes the marker but the mutation doesn't persist.
**Why:** `get_session` does not commit (project memory: mutating routers must `await session.commit()`).
**How to avoid:** the writer endpoint must `await session.commit()` itself. Test from an INDEPENDENT session.

## Code Examples

### `skipped_clause` builder (new, `services/stage_status.py`)
```python
# Source: mirrors done_clause/failed_clause house style (services/stage_status.py:169-227)
def skipped_clause(stage: Stage) -> ColumnElement[bool]:
    """Correlated force-skip marker probe for an ENRICH stage (D-08/D-13). Enrich-only."""
    if stage not in ELIGIBLE_AFTER_FAILURE:  # same guard shape as eligible_clause
        got = getattr(stage, "value", stage)
        raise ValueError(f"skipped_clause is defined only for the enrich stages ...; got {got!r}")
    return exists(select(StageSkip.id).where(StageSkip.file_id == FileRecord.id, StageSkip.stage == stage.value))
```

### DERIV-04 harness extension (`tests/integration/test_stage_status_equivalence.py`)
```python
# Source: extends the existing seed-fn + CASES/ELIGIBLE_CASES/DOMAIN_COMPLETED_CASES pattern (lines 148-337, 445-520)
async def seed_analysis_skipped_over_failed(session):   # the load-bearing skipped>failed precedence cell
    fid = await _new_file(session)
    session.add(AnalysisResult(file_id=fid, failed_at=datetime.now(UTC)))   # terminally-failed analyze
    await session.flush()
    session.add(StageSkip(file_id=fid, stage="analyze", reason="corrupt source"))  # + force-skip marker
    await session.flush()
    return fid

# Add cells:
#   CASES:                (Stage.ANALYZE, seed_analysis_skipped_over_failed, "skipped")   # skipped>failed
#   CASES:                (Stage.METADATA, seed_metadata_skipped, "skipped")
#   CASES:                (Stage.FINGERPRINT, seed_fp_skipped, "skipped")
#   ELIGIBLE_CASES:       (Stage.<enrich>, seed_<stage>_skipped, False)   # skipped leaves pending set
#   DOMAIN_COMPLETED_CASES:(Stage.<enrich>, seed_<stage>_skipped, True)   # recovery treats skipped as complete
# Also extend load_scalars() to read a `skipped` bool from stage_skip for the Python-twin side.
```

### Force-skip writer endpoint (new, enrich-only, D-09/D-10)
```python
# Source: mirrors the commit-before-return + validation discipline of the retry endpoints (pipeline.py:934)
@router.post("/pipeline/files/{file_id}/skip/{stage}", response_class=HTMLResponse)
async def force_skip_stage(file_id: uuid.UUID, stage: str, reason: Annotated[str, Form()], ...):
    if stage not in STAGE_TO_FUNCTION:            # D-10: enrich stages only (metadata/analyze/fingerprint)
        raise HTTPException(422, "stage not force-skippable")
    if not reason.strip():                        # D-09: reason required
        return <inline "A reason is required." fragment>
    reason = sanitize_pg_text(reason)             # project memory: NUL/free-text sanitizer before persist
    session.add(StageSkip(file_id=file_id, stage=stage, reason=reason))  # additive-only; never clears failed_at
    await session.commit()                        # get_session does NOT auto-commit
    # response: the stage pill flips to ⊘ skipped on next poll tick (not optimistic)
```
**Note:** apply `sanitize_pg_text` (project memory: `services/pg_text.py` — a NUL in free text passes
pydantic then aborts the PG transaction). The reason is persisted free text → must be sanitized before store.

### Priority stepper re-wire (PRIO-01, pure UI — endpoint is live)
```html
<!-- Source: routers/pipeline_stages.py:82 (delta is a Form field, x-www-form-urlencoded via hx-vals) -->
<button hx-post="/pipeline/stages/analyze/priority" hx-vals='{"delta": -10}'
        aria-label="Raise analyze priority">▲</button>   <!-- lower number = sooner (D-11 tooltip) -->
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Raw-enum `f.state` string in workspace tables | Derived 6-pill `stage_status_case` matrix | Phase 87 (this) | `metadata_workspace.html:50`, `analyze_workspace.html:81-86` retire the raw string |
| `state == FINGERPRINTED` counts (counted ~nothing) | `done_clause(FINGERPRINT)` file counts | Phase 82 (shipped) | Numbers visibly jump — the fix, not a regression |
| Failed bucket is a permanent dead-end | Force-skip → distinct `skipped` marker converges it | Phase 87 (this) | New escape hatch for genuinely-unprocessable files |
| Priority/pause steppers (Phase 38) removed in v7.0 | Re-wired onto the DAG rail (PRIO-01) | Phase 87 (this) | Endpoints were live-but-orphaned since v7.0 |

**Deprecated/outdated:** the Phase-38 stepper templates were removed in the v7.0 redesign — do NOT resurrect
the old templates; build fresh rail-node steppers against the still-live endpoints (they only need
`hx-post` + `hx-vals` + the `{stage, priority, paused}` response for re-render).

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | Sidecar `(file_id, stage)` table is preferable to per-stage columns | The `skipped` marker §1 | LOW — fingerprint's 1:N structure makes columns unworkable; sidecar is the only uniform shape. Verified against models. |
| A2 | Skip writer must be additive-only (never clear `failed_at`) to keep shadow-compare green | §4 + Pitfall 3 | LOW — verified by tracing the `analysis_failed` invariant predicate; but a plan that "tidies" `failed_at` would regress the gate. Test it. |
| A3 | Downstream unblocking of propose is NOT automatic (propose reader is state+completed_at based) | §3 / OQ-1 | MEDIUM — this is a genuine design fork the planner/user must resolve (see OQ-1). |
| A4 | Orphan/stuck count = ledger rows − live saq_jobs keys − domain_completed | UI-05 | MEDIUM — the exact "no progress" definition is a discretion call; verify against how `recover_orphaned_work` classifies candidates. |
| A5 | `sanitize_pg_text` applies to the skip reason | Writer example | LOW — project memory + `services/pg_text.py` precedent for persisted free text. |

## Open Questions (RESOLVED)

> **RESOLVED 2026-07-11 during plan-phase** (orchestrator, adopting the recommendations below):
> **OQ-1 → RESOLVED: SCOPE-MINIMAL** (option i) — force-skip converges the `failed` bucket + enrich
> pending sets + recovery, but does NOT wire propose-unblock; full-unblock is explicitly deferred to
> Phase 90. No plan touches `get_proposal_pending_batches`.
> **OQ-2 → RESOLVED: recovery-candidate count** — orphan/stuck = per-stage `(ledger − live − domain_completed)`,
> degrade-safe, matching what `recover_orphaned_work` would re-enqueue (no drift). Implemented in 87-08.
> **OQ-3 → RESOLVED: YES** — `stage_skip.stage` carries a CHECK restricting to the 3 enrich values.
> Implemented in 87-01.

1. **RESOLVED (SCOPE-MINIMAL): Does force-skipping analyze need to actually unblock propose? (the "downstream unblocking" fork)**
   - What we know: `eligible_clause` + `domain_completed_clause` extensions make skipped satisfy
     *eligibility* and *recovery*. But `get_proposal_pending_batches` (pipeline.py:1520) — the propose
     convergence reader — reads `state.in_([ANALYZED, METADATA_EXTRACTED])` AND
     `analysis_completed_at IS NOT NULL` **directly**, not through `done_clause`. A skipped analyze has
     `completed_at` NULL and `state='analysis_failed'`, so it will NOT flow into propose.
   - What's unclear: is "downstream unblocking" a hard requirement for THIS phase, or does the operator
     genuinely-unprocessable case (corrupt audio) actually WANT the file to stop (you can't propose a good
     name without analysis)? D-08's "downstream unblocking" language vs D-10's "terminally-failed analyze"
     target pull in different directions.
   - Recommendation: **surface to the user in discuss/plan.** Two coherent options: (i) SCOPE-MINIMAL —
     skip removes the file from the failed bucket + enrich pending sets + recovery (converges the `failed`
     view, honest `skipped` pill) but does NOT feed propose; downstream flow stays gated on real output.
     (ii) FULL-UNBLOCK — introduce a `stage_satisfied = done OR skipped` predicate and thread it into
     `get_proposal_pending_batches` (a bigger blast radius touching the propose reader, which is Phase-90
     territory). Recommend (i) for this phase's small-blast-radius rule, with (ii) explicitly deferred.

2. **RESOLVED (recovery-candidate count): Exact "orphaned/stuck" definition for UI-05.**
   - What we know: in_flight authority is `scheduling_ledger` (78 D-01); live jobs are `get_live_job_keys`
     (pipeline.py:566); recovery classifies candidates via `domain_completed_clause`.
   - What's unclear: whether "stuck" = "ledger row, no live saq_jobs job, not domain-completed" (= recovery
     candidate count) or adds a staleness threshold on `enqueued_at` (naive-timestamp footgun, Pitfall 4).
   - Recommendation: define it as the per-stage recovery-candidate count (ledger − live − domain_completed),
     degrade-safe via `_safe_count`, rendered per-stage on the rail. Confirm the definition matches what
     `recover_orphaned_work` would actually re-enqueue so the badge and recovery agree (no drift).

3. **RESOLVED (YES): Should `stage_skip.stage` carry a CHECK constraint restricting to the 3 enrich values?**
   - Recommendation: yes (belt-and-suspenders on D-10) — a DB CHECK `stage IN ('metadata','analyze','fingerprint')`
     backstops the writer's `STAGE_TO_FUNCTION` allowlist. Low cost; encodes the D-10 invariant durably.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| PostgreSQL (ephemeral :5433) | Migration 037 integration test + DERIV-04 harness | ✓ (test infra) | 16+ | — (tests `pytest.skip` when PG down — `test_stage_status_equivalence.py:89`) |
| `uv` toolchain | All commands (`uv run`) | ✓ | project constraint | — |
| SAQ/Redis (or PG broker) | Retry enqueue paths | ✓ (existing) | — | Retry endpoints already guard `NoActiveAgentError` |

No new external tools, services, or runtimes. This is a code + migration + template change.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest + pytest-asyncio (`uv run pytest`) |
| Config | `pyproject.toml` + `tests/buckets.json` (per-bucket isolation) |
| Quick run | `just test-bucket <bucket>` (metadata / fingerprint / analyze / shared / integration) |
| Full suite | `uv run pytest` (90% coverage floor; flakes under colima — re-run failed subset in isolation) |
| DB tests | `TEST_DATABASE_URL` / `PHAZE_QUEUE_URL` → the :5433 ephemeral DB (export BOTH; also `MIGRATIONS_TEST_DATABASE_URL` port 5433 for migration tests) |

### Phase Requirements → Test Map
| Req | Behavior | Test Type | Command | File Exists? |
|-----|----------|-----------|---------|-------------|
| UI-04/D-13 | skipped reads as DISTINCT bucket (skipped ≻ failed) | integration | `uv run pytest tests/integration/test_stage_status_equivalence.py -x` | ✅ extend CASES |
| UI-04/D-13 | skipped leaves the 3 enrich pending sets | integration | `... test_stage_status_equivalence.py -k eligible` + pending-set tests | ✅ extend ELIGIBLE_CASES + ❌ Wave 0 pending-set test |
| UI-04/D-13 | force-skip NOT re-enqueued by recovery | analyze/integration | `uv run pytest tests/analyze/tasks/test_recovery.py -x` | ❌ Wave 0 (add skipped cell) |
| UI-04/D-13 | DERIV-04 covers skipped on all 3 axes (status/eligible/domain_completed) | integration | `... test_stage_status_equivalence.py -x` | ✅ extend all 3 case lists |
| UI-04/D-13 | migration 037 up/down + empty autogenerate diff | integration | `uv run pytest tests/integration/test_migrations/test_037_*.py -x` | ❌ Wave 0 |
| UI-04 | shadow-compare stays green post-skip (additive writer) | integration | shadow-compare test with a skipped-file corpus | ❌ Wave 0 |
| UI-04 | writer commits + records reason + enrich-only 422 guard | metadata/router | router test (independent session read) | ❌ Wave 0 |
| UI-01 | pill matrix renders 6 stages; raw-enum "State" removed everywhere | shared/router | template render test + grep-guard for `f.state` render sites | ❌ Wave 0 |
| UI-01 | paginated files-table query does NOT whole-corpus scan | integration | assert LIMIT/OFFSET or keyset; no unbounded COUNT per poll | ❌ Wave 0 |
| UI-02 | per-file + bulk retry; analyze retry manual-only (no auto-loop) | analyze/router | reuse retry-endpoint tests + per-file scope | ❌ Wave 0 (per-file variant) |
| UI-03 | trace names conjuncts + blocker from ELIGIBILITY_DAG | shared | single-row `resolve_status` + trace-render test | ❌ Wave 0 |
| UI-05 | orphan count == recovery-candidate count (no drift); degrade-safe | integration | count helper test + `_safe_count` degrade | ❌ Wave 0 |
| PRIO-01 | steppers post to live endpoints; `{stage,priority,paused}` re-render | router | reuse `pipeline_stages` tests + rail render test | ✅ endpoint tested; ❌ rail wiring test |

### Sampling Rate
- **Per task commit:** `just test-bucket <bucket>` for the touched bucket (must pass in isolation —
  parallel-CI exposes non-hermetic tests: `get_settings` lru_cache leak + saq_jobs stub poison).
- **Per wave merge:** `uv run pytest` full suite green.
- **Phase gate:** full suite + DERIV-04 harness + shadow-compare green before `/gsd:verify-work`.

### Wave 0 Gaps
- [ ] `StageSkip` ORM model + `models/__init__.py` + Alembic `env.py` metadata registration.
- [ ] `tests/integration/test_migrations/test_037_stage_skip.py` — migration up/down + empty-diff.
- [ ] Extend `tests/integration/test_stage_status_equivalence.py` — skipped seed fns + cells on CASES /
      ELIGIBLE_CASES / DOMAIN_COMPLETED_CASES + `load_scalars` skipped read.
- [ ] Extend `tests/shared/test_stage_resolver.py` — `resolve_status` skipped branch + precedence.
- [ ] Pending-set tests (metadata/fingerprint/analyze buckets) — skipped file drops out of `eligible_clause`.
- [ ] `tests/analyze/tasks/test_recovery.py` — skipped file is domain-complete (not re-enqueued).
- [ ] Shadow-compare test with a skipped-file corpus (additive-writer green-gate proof).
- [ ] Files-table + pill-matrix render tests + raw-enum-removal grep guard (mutation-tested).
- [ ] Orphan-count helper test (no-drift vs recovery + degrade-safe).

## Security Domain

`security_enforcement` assumed enabled. This phase adds one mutating endpoint (force-skip) and re-exposes
control endpoints; the ASVS surface is small and well-precedented.

### Applicable ASVS Categories
| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | no (deferred) | Same reverse-proxy internal-realm auth as all `/pipeline/*` + `/saq` (T-37-04 precedent) — no app-layer auth added |
| V4 Access Control | yes | Force-skip restricted to enrich stages (D-10) — 422 on non-enrich stage, backstopped by a DB CHECK (OQ-3) so approval/execute can never be force-skipped (the "nothing moves without approval" core value) |
| V5 Input Validation | yes | `stage` validated against `STAGE_TO_FUNCTION` allowlist BEFORE use (T-37-01 precedent); `reason` required + `sanitize_pg_text` (NUL-abort footgun); `file_id` is a UUID path param (safe) |
| V6 Cryptography | no | No crypto in scope |

### Known Threat Patterns for FastAPI + SQLAlchemy + Jinja2
| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| SQL injection via stage/filter params | Tampering | Pure ORM + bound params + `STAGE_TO_FUNCTION` allowlist; NEVER f-string SQL (T-42-03 house rule) |
| XSS via file path / reason in templates | Tampering | Jinja2 autoescape; `_file_table.html` never pipes `text` through `| safe`; reason rendered as escaped text |
| Approval bypass via force-skip | Elevation of Privilege | D-10 enrich-only + DB CHECK; propose/review/apply have NO force-skip affordance |
| NUL byte in reason aborts PG txn | Denial of Service | `sanitize_pg_text` before persist (project memory: unbounded recovery loop otherwise) |
| Poll-time 500 on DB hiccup | Denial of Service | `_safe_count` / `begin_nested()` SAVEPOINT degrade on every new derived read (D-00c) |

## Project Constraints (from CLAUDE.md)

- **Python 3.14 exclusively; `uv` only** — never bare `pip`/`python`/`pytest`/`mypy`; always `uv run`.
- **Ruff** line-length 150, target `py313` (one behind runtime — PEP 649 deferred-annotation caveat);
  double quotes; `force-sort-within-sections`. `T201` print allowed only in CLI/tests.
- **Mypy strict** (excludes `tests/`, `services/`) — type hints on all functions; the new `StageSkip`
  model + `skipped_clause` must fully type-check.
- **90% coverage floor** (Codecov per-bucket flags); new writer + derivation code must be covered.
- **Pre-commit frozen SHAs**; bandit `-x tests -s B608`; **never `--no-verify`**.
- **Migrations:** sync `upgrade()`/`downgrade()`, mirrored `downgrade()`, integration test per migration
  in `tests/integration/test_migrations/`, **NEVER reference `saq_jobs`**, empty-autogenerate-diff contract
  (mirror the CHECK/index into ORM `__table_args__`).
- **Per-bucket test isolation** (`just test-bucket <bucket>`; must pass in isolation — non-hermetic-test hazard).
- **PR per phase / small blast-radius per PR**; worktree branches, no direct main commits.
- **`get_session` never auto-commits** — mutating routers commit themselves; tests assert from an
  independent session.

## Sources

### Primary (HIGH confidence — file:line verified in this session)
- `src/phaze/enums/stage.py` — `Stage`(7 members), `Status`(4-way), `ELIGIBILITY_DAG`, `FAILURE_IS_TERMINAL`,
  `ELIGIBLE_AFTER_FAILURE`, `resolve_status`, `eligible`, `domain_completed`.
- `src/phaze/services/stage_status.py` — `done_clause`/`failed_clause`/`inflight_clause`/`stage_status_case`
  (line 364, 4-way CASE)/`eligible_clause`(285)/`domain_completed_clause`(250); D-01 in_flight ledger authority.
- `src/phaze/services/shadow_compare.py` — INVARIANTS registry, implication-not-equality, soft allowlist
  (never grows past `{fingerprinted, local_analyzing}`), uses `done_clause`/`failed_clause` never `stage_status_case`.
- `tests/integration/test_stage_status_equivalence.py` — DERIV-04 harness (CASES:306, ELIGIBLE_CASES:502,
  DOMAIN_COMPLETED_CASES:445, `load_scalars`:351); the extension target.
- `src/phaze/models/analysis.py` / `metadata.py` / `fingerprint.py` — Phase-81 marker columns (1:1) vs
  fingerprint 1:N; XOR CHECK; partial indexes.
- `alembic/versions/032_add_derived_status_schema.py` / `033_add_analysis_completed_xor_failed.py` — the
  migration template (bare-name double-prefix caveat, additive backfill, mirrored downgrade, no saq_jobs).
- `src/phaze/routers/pipeline.py:934,1017` — live bulk retry endpoints (Phase-30 hardened, dedup-safe).
- `src/phaze/routers/pipeline_stages.py` — live priority/pause/resume endpoints (`{stage,priority,paused}`).
- `src/phaze/services/pipeline.py:1113,1151,1441,1461,1480,566,303` — failed/pending helpers, `get_live_job_keys`,
  `_safe_count`; `routers/pipeline.py:219-227` DAG-rail control overlay.
- `src/phaze/tasks/reenqueue.py` — reads exclusively through `domain_completed_clause` (recovery).
- `src/phaze/services/fingerprint.py:256` — `get_fingerprint_progress` (D-11 "numbers look different" note).
- `src/phaze/templates/pipeline/partials/_file_table.html` / `scan_status_pill.html` — reuse scaffolds.
- `src/phaze/models/scheduling_ledger.py:63` — `enqueued_at` naive TIMESTAMP (footgun).
- `src/phaze/tasks/_shared/stage_control.py:51` — `STAGE_TO_FUNCTION` (metadata→extract_file_metadata,
  analyze→process_file, fingerprint→fingerprint_file).

### Secondary (project memory — MEDIUM, cross-checked against code)
- `get_session` never commits; `sanitize_pg_text` mandatory for persisted free text; ledger `enqueued_at`
  naive-timestamp footgun; mutation-test guard tests (Phase 84); per-bucket CI isolation hazards.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new deps; every reused helper file:line-verified.
- `skipped` marker mechanics: HIGH — derivation composition traced end-to-end; sidecar shape forced by
  verified fingerprint 1:N structure.
- Downstream-unblocking scope (OQ-1): MEDIUM — a genuine design fork requiring user/plan resolution.
- Orphan-count definition (OQ-2): MEDIUM — needs alignment with recovery classification.
- UI re-wiring (matrix/filters/trace/steppers): HIGH — all backends live; UI-SPEC locks visuals.

**Research date:** 2026-07-10
**Valid until:** ~2026-08-10 (stable internal codebase; re-verify if Phases 88/90 land first and move the
propose reader or `files.state` deletion ahead of schedule).
