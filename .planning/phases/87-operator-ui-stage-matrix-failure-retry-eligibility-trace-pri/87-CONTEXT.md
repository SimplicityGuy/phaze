# Phase 87: Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority - Context

**Gathered:** 2026-07-11
**Status:** Ready for planning

<domain>
## Phase Boundary

The human-facing **operator console surface** over the derived `stage_status` layer that Phases 78/82
built. This phase replaces the raw-enum "State" string with a per-file **6-stage matrix** and gives the
operator: per-stage **failure visibility + retry**, a **"why not eligible?" trace** over the pure
`eligible()` conjuncts, a **force-done / skip** escape hatch so the `failed` bucket can converge, an
**orphaned-work count**, and the **restored per-stage priority stepper** (PRIO-01). All reads are
paginated / derived — **never a whole-corpus scan per 5s poll** (UI-01, PERF-01 anti-feature).

Requirements are locked by ROADMAP + REQUIREMENTS (UI-01..05, PRIO-01). This discussion decided **how it
looks and behaves**, not what to build.

**In scope:**
- **UI-01** — a per-file derived stage matrix (labeled pills) shown in a new **paginated files table**
  *and* an expanded form in the **per-file right pane**; retire the raw-enum "State" string
  (`templates/pipeline/partials/metadata_workspace.html:43,50`, `analyze_workspace.html:81-86`, and per-file views).
- **UI-02** — surface failed files per enrich stage via a **status filter on the files table**, with
  **per-file and bulk-per-stage** retry (re-wiring the live `POST /pipeline/analysis-failed/retry` and
  `POST /pipeline/metadata-failed/retry`; failed fingerprints already auto-retry via the pending set).
- **UI-03** — an on-demand per-stage **eligibility trace** in the right pane (click a stage pill →
  named-conjunct pass/fail breakdown + the specific blocker).
- **UI-04** — a **force-done / skip** control (enrich stages only) that writes a **distinct per-stage
  `skipped` marker** (new writer + migration; see D-13 — the meaningful non-UI slice of this phase),
  guarded by a confirm dialog + reason note.
- **UI-05** — an **orphaned/stuck-work count** (in-flight marker, no progress; derived from the ledger)
  surfaced as a **DAG-rail badge** near the affected stage.
- **PRIO-01** — re-wire the per-stage **priority stepper + pause/resume** to the still-live
  `POST /pipeline/stages/{stage}/{priority,pause,resume}` endpoints (`routers/pipeline_stages.py`),
  placed on the DAG rail, with clarified labeling.

**Out of scope:**
- Any change to the derived-status **derivation contract itself** beyond adding the new `skipped` marker
  read — `done_clause`/`failed_clause`/`inflight_clause`/`stage_status_case`/`eligible_clause` are owned
  by Phases 78/82 and consumed here.
- The `files.state` column drop / `FileState` enum deletion / remaining `.state=` writers — **Phase 90**.
- Lane / agent drill-in views — **Phase 88** (DRILL-01..03).
- Any change to routing *policy*, approval semantics, or the tag/CUE bulk builders.

</domain>

<decisions>
## Implementation Decisions

### Upstream contract (carried forward — do not re-litigate)
- **D-00a: `stage_status_case(stage)` is the single 4-bucket CASE** (`not_started / in_flight / done /
  failed`) — the pill matrix, the failure filter, and the four-bucket counts all read the **same**
  definition (`services/stage_status.py`). No second status-derivation path. (Phase 82 D-04.)
- **D-00b: Failed *analyze* is terminal** — manual retry only (the 44.5K `recover_orphaned_work`
  over-enqueue guard, `ELIGIBLE_AFTER_FAILURE[ANALYZE] = False`). Failed **metadata/fingerprint**
  auto-retry. The retry UI must not offer an auto-retry path for analyze. (Phase 82 D-00d.)
- **D-00c: Never render a whole-corpus scan per poll** — the files table is paginated (keyset/offset),
  and every derived read stays `_safe_count`/SAVEPOINT-degrade-safe (never 500 the 5s poll). (PERF-01/02.)

### Stage matrix form & home (UI-01)
- **D-01: Matrix form = a row of labeled pills** — 6 pills in stage order (Meta / FP / Analyze / Propose
  / Approve / Execute), each labeled and colored by its `stage_status_case` bucket
  (✓ done · ● in-flight · — not-started · ✗ failed). Self-documenting, wraps on narrow screens, reuses the
  existing pill/badge tokens (`scan_status_pill.html` pattern).
  (Rejected: compact colored dots — denser but less self-evident; segmented progress bar — reads as
  progress but weak for pinpointing a specific stage.)
- **D-02: Home = BOTH a paginated files table AND the right pane.** The files table (path + pill matrix,
  paginated) is the scannable "where's this file at?" overview; the v7.0 shell's per-file right pane holds
  the **expanded** matrix with the eligibility trace (D-05/D-06) and the force-done/skip controls
  (D-11..D-13). Reuse the `_file_table.html` scaffold.
  (Rejected: right-pane-only — no scannable overview; table-only — no home for the trace/force-done
  controls.)

### Failure + orphan surfacing / retry (UI-02, UI-05)
- **D-03: Failed files surface as a status filter on the files table**, not a separate failures page or
  per-workspace Failed tabs. "Show files where {stage} = failed" is just another lens on the same
  paginated list + pill matrix — one canonical surface. (Rejected: per-workspace Failed tabs — three
  places to maintain; dedicated failures view — diverges from stage-centric shell nav.)
- **D-04: Retry granularity = BOTH per-file and bulk-per-stage.** Per-file retry button on each failed
  row + a "retry all failed in this stage" bulk action. The existing `analysis-failed/retry` and
  `metadata-failed/retry` endpoints are bulk-shaped over the failed set; per-file is a scoped variant.
  Analyze bulk-retry must respect the terminal-analyze guard (D-00b) — it retries the manual-analyze
  failed set, not an auto-loop.
- **D-05: Orphaned/stuck-work count = a DAG-rail badge** near the affected stage (matches the rail's
  existing live-count pattern; ambient, always visible from anywhere in the shell). Derived for free from
  the chosen `in_flight` source (ledger row present, no progress). (Rejected: header status strip —
  reads as system-health not per-stage; Analyze-workspace card — only visible on that page.)

### "Why not eligible?" trace (UI-03)
- **D-06: Trace trigger = per-stage, in the right pane.** Clicking a stage pill in the expanded right
  pane reveals *that* stage's eligibility trace — ties the diagnostic to the pill the operator is curious
  about, fits the D-02 expanded-pane decision. (Rejected: always-on full trace block — denser to render;
  hover tooltip — too cramped for a 4-conjunct explanation, poor on touch.)
- **D-07: Trace depth = named conjuncts + the specific blocker.** Each conjunct as a pass/fail line
  (`done?` · `in-flight?` · `upstream met?` · `terminally-failed?`) AND name the blocker when unmet — e.g.
  `upstream met? ✗ metadata not done ← blocker`. Actionable: tells the operator exactly what to fix. Reads
  the `eligible()` conjuncts + the `ELIGIBILITY_DAG` for the upstream name. (Rejected: plain checklist —
  omits which upstream; one-line verdict — hides the conjunct breakdown that makes the trace trustworthy.)

### Force-done / skip escape hatch (UI-04)
- **D-08: Semantics = a distinct `skipped` marker.** Force-done/skip writes a **per-stage skip marker**
  (a sidecar, analogous to the Phase-81 failure markers) that the derivation treats as **stage-satisfied**
  for eligibility + downstream unblocking, but reports as a **distinct `skipped` pill** — NOT counterfeit
  `done`. Preserves derive-don't-store AND honesty: a forced-skip is always distinguishable from genuine
  completion, forever, for audit/debug. (Rejected: synthesizing a real output-table row — counterfeits
  data, a forced analyze becomes indistinguishable from a real one; "you decide" — user chose the honest
  marker explicitly.)
- **D-09: Guard = confirm dialog + reason note.** Per-file only; requires a confirm step and a short
  free-text **reason recorded with the marker** (audit trail for "why did this file get skipped?").
  Deliberate friction on a rarely-used, consequential, hard-to-reverse action. (Rejected: confirm-only —
  loses the why; one-click — too easy to misfire.)
- **D-10: Scope = enrich stages only** (metadata / fingerprint / analyze). These are the stages that
  strand files — especially terminally-failed analyze, the genuinely-unprocessable case UI-04 targets.
  Downstream propose/approve/execute are human-gated and must NOT be force-skippable (skipping approval
  would move/rename files without review — violates the core "nothing moves without approval" value).
  (Rejected: all six — approval-bypass hazard; analyze-only — too narrow, metadata/fingerprint can also
  wedge on a genuinely-bad file.)

### Priority stepper (PRIO-01)
- **D-11: Re-wire BOTH the priority stepper AND pause/resume**, per-stage on the DAG rail (where Phase 38
  placed them, removed in the v7.0 redesign), wired to the still-live `POST /pipeline/stages/{stage}/priority`,
  `/pause`, `/resume` endpoints (`routers/pipeline_stages.py`). Both endpoints are orphaned, so both come
  back (PRIO-01's "if likewise orphaned" clause). Add a **clarifying label/tooltip** so "▲ raises priority
  = lowers the number" is not confusing. Response returns `{stage, priority, paused}` from the durable
  control row for HTMX re-render (existing contract). Captured as the accepted default; not deep-discussed.

### The new `skipped` marker — the non-UI slice (D-13)
- **D-13: Phase 87 is NOT purely presentational.** D-08's distinct `skipped` marker requires, before any
  UI: (a) a **schema + Alembic migration** for the per-stage skip marker (mirror the Phase-81 failure-marker
  shape / ≤1-row invariant; sync migration, mirrored `downgrade()`, an integration test in
  `tests/integration/test_migrations/`, never reference `saq_jobs`); (b) a **writer** behind the force-skip
  endpoint that stamps the marker + the D-09 reason; (c) a **read** in the derivation layer so
  `stage_status_case`/`eligible_clause` surface `skipped` as stage-satisfied AND as its own reported bucket
  — **drift-locked via the Phase-78 DERIV-04 SQL⇔Python equivalence harness** (extend it, don't bypass it);
  (d) the Phase-79 **shadow-compare gate must stay green** across the change. This is the phase's sharpest
  correctness surface — research + planning own the marker mechanics; the equivalence harness is
  non-negotiable.

### Claude's Discretion
- **Files-table default scope + filter set** — default listing (all music/video? a sensible recent/active
  slice?), the available filters (per-stage bucket, file type, path search), and pagination style
  (keyset vs offset) — planning's call, constrained by D-00c (never a whole-corpus scan per poll).
- **Pill labels & bucket color tokens** — exact abbreviations (Meta/FP/…) and the 4-bucket palette,
  reusing existing tokens; the `skipped` pill's distinct visual treatment (D-08).
- **Right-pane layout** — how the expanded matrix, the per-stage trace (D-06/D-07), and the force-skip
  controls (D-09) compose in the pane.
- **Retry response shape** — reuse `metadata_retry_response.html` / `retry_failed_response.html`
  partials vs new ones; the bulk-vs-per-file HTMX affordances.
- **Plan/PR decomposition** — natural seams: (a) the `skipped` marker schema+migration+writer+derivation
  read + DERIV-04 harness extension (D-13); (b) the paginated files table + pill matrix + status filters
  (UI-01/UI-02 surface); (c) the right-pane expanded matrix + eligibility trace (UI-03) + force-skip
  controls (UI-04); (d) the orphan-count badge (UI-05) + priority/pause/resume re-wire (PRIO-01). Small
  blast-radius per PR is the milestone's standing rule.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Milestone design contract & requirements
- `.planning/milestones/PARALLEL-ENRICH-DAG-DESIGN.md` §7 — names the raw-enum "State" UI as cutover
  surface (`metadata_workspace.html:43,50`, `analyze_workspace.html:81-86`); §5 (YAGNI / derive-don't-store);
  §8 (constraints: 90% cov, per-bucket test isolation, sync migrations); the G-03 (force-skip convergence)
  / G-04 (eligibility-trace diagnostic) gaps this phase closes.
- `.planning/REQUIREMENTS.md` — UI-01..05 + PRIO-01 (full text); PERF-01 (no whole-corpus poll); the
  anti-feature table ("rendering raw internal status strings", "a stats poll that scans the whole corpus").
- `.planning/ROADMAP.md` §"Phase 87" — goal + 5 success criteria; depends on Phase 82, Phase 78.

### Upstream phase contracts (locked decisions — do not re-litigate)
- `.planning/phases/78-derivation-layer-eligibility-anti-drift-test-harness/78-CONTEXT.md` — the
  single-source predicate module (`enums/stage.py` + `services/stage_status.py`); the **DERIV-04 SQL⇔Python
  equivalence harness** the new `skipped`-marker read MUST extend (D-13); `ELIGIBLE_AFTER_FAILURE` semantics.
- `.planning/phases/81-per-stage-failure-persistence-retry-paths/81-CONTEXT.md` — the per-stage failure
  markers (`metadata.failed_at`, analyze failure) the `failed` bucket + retry read; the **shape the new
  `skipped` marker mirrors** (D-08/D-13).
- `.planning/phases/82-counts-pending-set-cutover/82-CONTEXT.md` — `stage_status_case` as the single
  4-bucket CASE (D-04 there); the four-bucket `get_stage_progress`; `eligible_clause(stage)`; the
  mutation-tested divergence-guard discipline the force-skip read must satisfy.

### Source of truth in code
- `src/phaze/enums/stage.py` — `Stage`, `Status`, `ELIGIBILITY_DAG`, `ELIGIBLE_AFTER_FAILURE`,
  `eligible()` (the conjuncts the D-07 trace renders + names the blocker from).
- `src/phaze/services/stage_status.py` — `done_clause`/`failed_clause`/`inflight_clause`/
  `stage_status_case`/`eligible_clause` (the pill matrix + failure filter + trace read from these; the new
  `skipped` marker read lands here, D-13).
- `src/phaze/services/pipeline.py` — `:302` `get_stage_progress` (four-bucket counts), `:1113`
  `get_analysis_failed_files`, `:1124` `get_analysis_failed_count`, `:1461` `get_metadata_failed_files`;
  the paginated files-table query lives near here.
- `src/phaze/services/fingerprint.py:257` — `get_fingerprint_progress` (derived total/completed/failed;
  failed fingerprints auto-retry via the pending set — no manual retry endpoint needed).
- `src/phaze/routers/pipeline.py` — `:934` `POST /pipeline/analysis-failed/retry`, `:1017`
  `POST /pipeline/metadata-failed/retry` (D-04 re-wire targets); `:219-227` the DAG per-stage
  priority/pause overlay (PRIO-01 render target).
- `src/phaze/routers/pipeline_stages.py` — `POST /pipeline/stages/{stage}/{priority,pause,resume}` (the
  live-but-orphaned PRIO-01 endpoints; response `{stage, priority, paused}` from the durable control row).
- `src/phaze/templates/pipeline/partials/` — `_file_table.html` (files-table scaffold to reuse),
  `metadata_workspace.html:43,50` + `analyze_workspace.html:81-86` (raw-enum "State" to retire),
  `scan_status_pill.html` (pill token pattern for D-01), `metadata_retry_response.html` /
  `retry_failed_response.html` (retry response partials).
- `src/phaze/services/shadow_compare.py` — the Phase-79 gate that must stay green across the new marker (D-13).
- Migrations: `tests/integration/test_migrations/` (the per-migration integration-test home for the new
  `skipped`-marker revision, D-13).

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **`stage_status_case(stage)`** (`services/stage_status.py`) — the single 4-bucket CASE; the pill matrix,
  the failed-status filter, and the four-bucket counts all read it. Extend (don't fork) for the `skipped`
  bucket (D-08/D-13).
- **`_file_table.html`** — existing paginated file-table scaffold (thead/tbody + empty-state) to host the
  pill matrix (D-02).
- **`scan_status_pill.html`** — the existing status-pill token to base the D-01 stage pills on.
- **Retry endpoints already live** — `analysis-failed/retry`, `metadata-failed/retry` are bulk-shaped over
  the failed set; per-file retry (D-04) is a scoped variant. Fingerprint failures auto-retry via the
  pending set (`get_fingerprint_pending_files`) — no new fingerprint retry backend.
- **Priority/pause/resume endpoints already live** — `routers/pipeline_stages.py`; PRIO-01 is a pure UI
  re-wire of Phase 38's removed control to the still-mounted endpoints.
- **Phase-81 failure-marker shape** — the ≤1-row per-stage marker pattern the new `skipped` marker mirrors.

### Established Patterns
- **DERIV-04 equivalence harness** (Phase 78) — parametrized SQL-derived == Python-derived across the
  fixture matrix; the `skipped`-marker derivation read extends it (D-13). Bypassing it recreates the exact
  drift class this milestone exists to prevent.
- **Mutation-tested divergence guard** (Phase 84, standing rule) — a green guard proves nothing; construct
  a corpus where the marker and `state` disagree, break source, watch RED, restore. Applies to the
  `skipped`-marker read.
- **`_safe_count` / `begin_nested()` SAVEPOINT degrade** — every derived read on the 5s poll degrades to
  0/None rather than 500. The files-table + counts + orphan badge all inherit this.
- **Sync Alembic migration + mirrored `downgrade()` + integration test, never reference `saq_jobs`** — the
  new `skipped`-marker revision follows this (D-13).

### Integration Points
- The raw-enum "State" render (`metadata_workspace.html:43,50`, `analyze_workspace.html:81-86`, per-file
  views) is retired in favor of the pill matrix — confirm every raw-enum render site is migrated.
- The force-skip writer + marker composes into `stage_status_case`/`eligible_clause`, so it changes what
  the three enrich pending sets enqueue (a skipped file must leave the pending set) — verify against the
  recovery/re-enqueue path and the manual triggers (Phase-42 "UI/API/recovery must not drift" precedent).
- The DAG-rail hosts the orphan-count badge (D-05) AND the priority/pause/resume steppers (D-11) — one rail,
  two additions.

</code_context>

<specifics>
## Specific Ideas

- **Pill matrix mock (D-01):** `[Meta ✓][FP ●][Analyze —][Prop —][Appr —][Exec —]` with a legend
  `✓=done ●=in-flight —=not-started ✗=failed` (+ a distinct `skipped` treatment per D-08).
- **Eligibility-trace mock (D-07):**
  ```
  Analyze — NOT eligible
    done?           ✗ not done
    in-flight?      ✗ no
    upstream met?   ✗ metadata not done  ← blocker
    terminal fail?  ✗ no
  ```
- **Honesty over convenience for force-skip (D-08):** the user explicitly chose the distinct `skipped`
  marker over the simpler "synthesize a real output row" — a forced-skip must never be mistakable for
  genuine completion. Preserve this in the pill, the counts, and any export.
- **Numbers will look different, not broken** — as failed/skipped buckets and simultaneous per-stage
  eligibility become visible, counts shift relative to the old serially-gated view. Say so in the SUMMARY.

</specifics>

<deferred>
## Deferred Ideas

- **Lane / agent drill-in views** — clickable lane/agent detail over the new `stage_status` → **Phase 88**
  (DRILL-01..03).
- **`files.state` column drop + `FileState` enum deletion + remaining `.state=` writers** → **Phase 90**.
- **DENORM-01 — denormalized stored stage-bitmap column** — only if a poll-time measurement proves the
  derived files-table query too slow. YAGNI; carried from Phase 82 D-07.

### Reviewed Todos (not folded)
- **`analysis-completed-at-backfill.md`** ("1001 production `analyzed` rows fail the shadow gate") —
  reviewed, **not folded**: already resolved upstream by Phase 80's migration `036` (Phase 82 D-02). This
  UI phase does not touch the backfill; keyword-only match (score 0.6).
- **`wr-01-review-builder-limit-before-filter.md`** ("Tag/CUE bulk builders apply `.limit()` before the
  qualifying-change filter — 200K starvation") — reviewed, **not folded**: a backend query-correctness bug
  in the tag/CUE bulk builders, a different code path from this phase's files-table/stats surface. Its
  "200K starvation" theme rhymes with UI-01's no-whole-corpus-scan rule but is out of scope here → its own
  quick task. Keyword-only match (score 0.6).

</deferred>

---

*Phase: 87-Operator UI — Stage Matrix, Failure Retry, Eligibility Trace & Priority*
*Context gathered: 2026-07-11*
