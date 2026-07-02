# Phase 61: Full record + ⌘K + Agents - Context

**Gathered:** 2026-07-01
**Status:** Ready for planning

<domain>
## Phase Boundary

Additive depth over the now-live v7.0 shell (Phases 57–60). Four surfaces, all **composing existing partials/endpoints** with **one new CDN dep** (`@alpinejs/focus@3.15.12`, locked by ROADMAP). Delivers RECORD-01..04:

1. **RECORD-01 — Per-file full record.** A **wide slide-in panel over the shell** (right-anchored overlay, backdrop, `x-trap.inert.noscroll` focus-trap) opened from a file row or ⌘K. Sections per design §8: header (name/path/format/size/sha256/lane) · windowed multi-lane analysis timeline (BPM/key/energy over the set's windows, builds on Phase 31/57.1) · metadata diff · identity (track-ID / tracklist match / proposed name) · **this file's pending approvals (inline-approvable)** · history.
2. **RECORD-02 — ⌘K command palette.** Grouped unified search (Files / Tracklists / Artists) over the existing search service + quick commands, with full keyboard nav. Fills the Phase 57 skeleton (`cmdk_modal.html`), funneled through the existing search service and `enqueue_router` guards.
3. **RECORD-03 — Agents page.** local/A1 as heartbeating `Agent` rows; the **k8s burst lane as an ephemeral, Job-based identity** (liveness synthesized from in-flight `CloudJob`/Kueue workloads) in a **separate section** — never a perpetually-DEAD agent (carries v6.0 KDEPLOY-04 into the new UI).
4. **RECORD-04 — First-run empty state.** When file count == 0, a centered guide reusing the **existing agent `scan_roots` + scan trigger** (no new input surface), with live scan progress riding the existing poll.

**Milestone rule (still holds):** v7.0 is an IA/presentation rewrite — analysis, identify, proposal, and execution **logic** are unchanged (REQUIREMENTS.md line 82). This phase composes existing endpoints; the only additive backend touch sanctioned is a **read-only distinct-artist SELECT** for the ⌘K Artists group (D-05) — a read query, not a logic change.

**Cross-cutting constraints (locked, do not re-litigate):** the record + palette must ride the **single existing `/pipeline/stats` 5s poll — add no new loop**; `@alpinejs/focus@3.15.12` loaded `<script defer>` **before** Alpine core, version exactly matching Alpine core, `x-trap.inert.noscroll` for both the palette and the slide-in; supersede-in-place (dead-template removal is Phase 62); C3 aesthetic + all Phase 57 shell contracts (`#stage-workspace` swap target, fragment-only responses, `oob_counts` gate, `$store.pipeline` consumed-not-redefined, `/s/<stage>` scheme, theme/brand).

**Explicitly NOT this phase:** a11y depth / keyboard-rail parity / skip-link / DAG ARIA, narrow-width rail collapse, dead legacy-template/router removal (all Phase 62 / CUT-01..04); the empty aside removal (`shell.html:164`) rides Phase 62 cleanup once the slide-in supersedes it.

**UI contract:** No 61-UI-SPEC.md exists yet. This CONTEXT captures surface/data-wiring/scope-edge decisions; the visual contract (slide-in layout, palette grouping/keyboard affordances, Agents two-section layout, empty-state copy) is produced next by `/gsd:ui-phase 61`, inheriting the 57/58/59/60 design system verbatim.
</domain>

<decisions>
## Implementation Decisions

### Full record surface + liveness (RECORD-01)
- **D-01: Wide slide-in overlay only.** The full record is a single **wide, right-anchored slide-in panel over the shell** (backdrop dim, `x-trap.inert.noscroll`), opened from a file row and from ⌘K. It is **the only per-file surface** — the existing empty 350px right `<aside>` (`shell.html:164`) is not used for a preview tier (its removal is a Phase 62 cleanup). Matches design §5/§8 + `prototype.html` literally. Serve the record as an HTMX **fragment** (mirror the fragment-only stage convention) — planner picks the route shape (e.g. `/record/{file_id}` or `/s/…` analog); no `extends base.html`.
- **D-02: Open record is a snapshot whose COUNTS/STATUS ride the existing poll.** The record body renders once on open (static structure — no self-refresh request). A few OOB-targeted bits — this file's stage chips / lane badge / pending-approval count — update off the **same `/pipeline/stats` 5s fanout** via `hx-swap-oob` behind the `oob_counts` gate. **No new loop.** This mirrors Phase 60's "counts-only OOB" discipline: **never re-render the operator's in-progress subtree** (e.g. an approval selection inside the open record). New OOB ids for the open record must register in the existing registry behind the gate.

### ⌘K command palette (RECORD-02)
- **D-03: Search groups = Files / Tracklists / Artists; four quick commands.** Typed search funnels through the existing `search_queries.search()` (files + tracklists already returned; Discogs results already exist — planner decides whether to surface them or keep to the three named groups). Quick commands (all four selected): **Scan** (fires the existing `pipeline.py` scan trigger via `enqueue_router`), **Jump to a stage** (rail nav to any `/s/<stage>`), **Jump to a review queue** (into a specific Phase 60 Review & Apply gate), **Open Agents** (the Agents page).
- **D-04: Full arrow-nav + grouped palette.** Results and commands render in labeled groups (Files / Tracklists / Artists / Commands); `↑`/`↓` moves the active row **across** groups, `Enter` activates the active item (open record · run command · navigate), `Esc` closes and returns focus to `#cmdk-trigger`. Full command parity (design §13 open question resolved toward parity). Focus-trap via `@alpinejs/focus` `x-trap.inert.noscroll`.
- **D-05: Artists group = read-only distinct-artist query; Enter filters files by that artist.** The search service has **no `artist` result_type** — artist is a field/filter (`FileMetadata.artist`, `Tracklist.artist`, and the FTS `concat_ws`). The Artists group is populated by a **read-only `SELECT DISTINCT` aggregation** of `FileMetadata.artist` / `Tracklist.artist` matching the query (optionally with a per-artist file count). `Enter` on an artist filters the file list via the **existing `artist=` query param** (`search.py` / `search_queries.search(artist=…)`). This is the **one sanctioned additive backend touch** — a read query, consistent with "presentation-only" (no analysis/proposal/execution logic changes). Planner: confirm the distinct query is cheap/indexed; keep it read-only.

### Agents page (RECORD-03)
- **D-06: Two sections — heartbeating Agents + ephemeral Compute lanes.** Section 1 = local/A1 as today (`admin_agents.py` table + `agent_liveness.classify()` / `sort_key`). Section 2 = a distinct **"Compute / burst lanes"** section for k8s, driven by **`CloudJob` in-flight workload counts** (not `Agent` rows / not `last_seen_at`). This keeps the ephemeral, non-heartbeat nature visually honest (KDEPLOY-04 intent) rather than implying k8s is a persistent agent.
- **D-07: k8s liveness = Active / Waiting / Idle (never DEAD), from `CloudJob`.** Derive from existing `CloudJob` columns (read-only): **ACTIVE** when ≥1 workload `status=running`; **WAITING (quota)** when submitted-but-`inadmissible=true` (Kueue quota-wait, reusing Phase 58's quota-wait surfacing); **IDLE** when no in-flight workloads. **Never DEAD.** Show the in-flight count when Active. No backend logic change — a read query/aggregation over `CloudJob`.

### First-run empty state (RECORD-04)
- **D-08: Agent-roots-only guide — no new path input, no directory browser.** When file count == 0, show a centered card listing each registered agent + its configured `scan_roots`, a **"Scan {agent}"** button (the **existing** scan trigger via `enqueue_router`), and a **"Configure roots →"** link to agent config. **Zero new input surface** — no free-text path field, no server-side directory-browsing endpoint (deliberately avoiding a filesystem-listing attack surface on the archive host). Live scan progress rides the **existing `/pipeline/stats` poll** (no new loop). Placement (home/Analyze workspace when count==0) is discretion.

### Claude's Discretion
- The exact route shape for the record fragment (`/record/{file_id}` vs a `/s/…` analog) and the OOB ids registered for the open record (must ride the single poll behind `oob_counts` — no second loop).
- Whether Discogs-release results appear in ⌘K or the palette is limited to the three named groups (Files/Tracklists/Artists) + Commands.
- The empty-state placement (home/Analyze workspace vs a dedicated fragment) and its copy (the `/gsd:ui-phase 61` UI-SPEC locks it; mirror 58/59/60 locked-copy approach).
- Whether "history" in the record reads from `ExecutionLog` + `TagWriteLog` (per-file audit rows) directly or via the existing `/audit/` view scoped to the file — pick whichever composes cleanest read-only.
- Reuse vs restyle of any legacy per-file partials (`proposals/partials/row_detail.html`, `tracklists/partials/track_detail.html`) into the record's sections — supersede-in-place; legacy templates stay until CUT-02.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### This phase's contracts
- `.planning/ROADMAP.md` § "Phase 61: Full record + ⌘K + Agents" — Goal, 4 Success Criteria, and the **Notes** block (the `@alpinejs/focus@3.15.12` load-order rule, `x-trap.inert.noscroll` for palette + slide-in, "ride the existing single poll — add no new loop", and "verify the ⌘K 'artists' facet maps to existing search fields at plan time").
- `.planning/REQUIREMENTS.md` § "Full record + ⌘K + Agents (RECORD)" — RECORD-01..04 (lines 60–63; mapping table 114–117). Line 82 = the milestone "logic unchanged" rule (scoped to logic — the D-05 distinct-artist SELECT and D-07 CloudJob aggregation are read queries, within scope).

### Design & IA (authoritative, inherited from v7.0)
- `docs/superpowers/specs/2026-06-28-ui-redesign-dag-console-design.md` — §5 IA ("Full record opens as a slide-in over the shell"), §8 Per-file full record (the section list D-01 realizes), §9 Global surfaces (⌘K command set + ephemeral k8s Agents framing + empty/first-run), §10 cloud/k8s integration (lane badges, quota-wait vs Inadmissible), §13 open questions (⌘K keyboard depth — resolved in D-04).
- `docs/superpowers/specs/2026-06-28-ui-redesign-assets/prototype.html` — canonical interactive reference for the full record slide-in, ⌘K palette, Agents page, and empty/scan first-run. Match layout/behavior.

### Pattern to reuse (immediate predecessors 57–60 — do not re-litigate)
- `.planning/phases/57-shell-dag-rail/57-CONTEXT.md` + `57-UI-SPEC.md` — locked shell contracts (`#stage-workspace`, fragment-only responses, single `/pipeline/stats` poll + `hx-swap-oob` + `oob_counts` gate, `$store.pipeline` consumed-not-redefined, `/s/<stage>` scheme, theme/brand); the ⌘K **skeleton** (D-04 there) this phase makes functional.
- `.planning/phases/60-review-apply/60-CONTEXT.md` — the Review & Apply gate whose rows carry inline Approve/Edit/Skip; RECORD-01's "this file's pending approvals (inline-approvable)" reuses the **same approve endpoints + `_diff_row.html`** those rows use. Phase 60 explicitly deferred "row-click → rich per-file record slide-in" to this phase.

### Data model + endpoints verified at discuss time
- `src/phaze/routers/search.py` (`search_page`, `artist` query param) + `src/phaze/services/search_queries.py` (`SearchResult` = result_type file/tracklist/discogs_release + `title`/`artist`; FTS `concat_ws(original_filename, artist, title, genre)`; `artist` ilike filter; `union_all` of files/tracklists/discogs; `get_summary_counts`). **⌘K search source (D-03); the D-05 distinct-artist query targets `FileMetadata.artist` / `Tracklist.artist`.**
- `src/phaze/templates/shell/partials/cmdk_modal.html` — the Phase 57 skeleton (open/close/`$nextTick` focus, `?palette=1`, ESC → `#cmdk-trigger`) this phase wires (D-03/D-04).
- `src/phaze/templates/shell/shell.html` — the three-column shell; `:160` `#stage-workspace`; `:164` the empty 350px right `<aside>` (D-01 does NOT use it); `:172` `{% include cmdk_modal %}`; the `htmx:historyRestore` re-init handler.
- `src/phaze/routers/admin_agents.py` (`_load_agents`, `page`, `table_partial`) + `src/phaze/services/agent_liveness.py` (`classify` → dead/stale/alive by `AGENT_LIVENESS_ALIVE_SECONDS`; `sort_key`). **Section 1 of the Agents page (D-06).**
- `src/phaze/models/agent.py` — `Agent` = `kind` (`fileserver`/`compute`), `scan_roots` (JSONB), `last_seen_at`, `last_status`. **Heartbeating agents (D-06) + scan_roots for the empty state (D-08).**
- `src/phaze/models/cloud_job.py` — `CloudJob` = `file_id` + `s3_key` + `status` (`CloudJobStatus`: uploading/uploaded/submitted/running/succeeded/failed) + `kueue_workload` + `inadmissible` (bool) + `cloud_phase`. **k8s ephemeral liveness source (D-06/D-07): running→ACTIVE, inadmissible→WAITING, none→IDLE.**
- `src/phaze/models/analysis.py` — `AnalysisResult` (`bpm`, `fine_windows_analyzed/total`, `coarse_windows_analyzed/total`) + `AnalysisWindow` (`:41`, migration 018 — per-window `bpm`/`musical_key`, fine/coarse tiers). **The record's windowed multi-lane timeline (D-01).**
- `src/phaze/templates/pipeline/partials/_diff_row.html` — the shared before→after diff row (Phase 60) reused for the record's metadata diff + approval rows.
- `src/phaze/routers/pipeline.py` — `/pipeline/scan-live-sets` (`:1187`) + `_enqueue_scan_jobs` (`:1165`) via `enqueue_router` — the scan trigger for ⌘K "Scan" (D-03) and the empty state (D-08); `/pipeline/stats` 5s poll + `oob_counts` gate the record + palette ride (D-02).
- `src/phaze/routers/proposals.py` + `src/phaze/routers/tags.py` — existing per-file approve/reject/undo + tag-write approve the record's inline pending-approvals reuse (RECORD-01; same paths as Phase 60).
- `src/phaze/models/execution.py` (`ExecutionLog`) + `src/phaze/models/tag_write_log.py` (`TagWriteLog`) + `src/phaze/routers/execution.py` `/audit/` — the record's **history** section source (read-only; D-01, Discretion).
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- **⌘K skeleton** (`shell/partials/cmdk_modal.html`): open/close/keybinding/`$nextTick`-focus/`?palette=1`/ESC-returns-focus already built (Phase 57 D-04) — this phase adds search groups + commands + arrow-nav + `x-trap`.
- **Search service** (`search_queries.py`): files + tracklists + discogs already returned with `artist`/`genre`/date filters + FTS — ⌘K reuses verbatim; only add a read-only distinct-artist SELECT (D-05).
- **Agent liveness** (`agent_liveness.classify`/`sort_key` + `admin_agents.py`): heartbeating-agents section reused as-is; k8s is a *new synthetic* section from `CloudJob` (no Agent row).
- **CloudJob columns** (`status`, `inadmissible`, `kueue_workload`): everything needed for Active/Waiting/Idle already persisted (Phases 53–55) — a read aggregation, no schema/logic change.
- **Windowed analysis** (`AnalysisWindow`, migration 018 / Phase 31 / 57.1): per-window bpm/key powers the record's multi-lane timeline.
- **`_diff_row.html`** (Phase 60) + proposals/tags approve endpoints: the record's metadata diff + inline approvals reuse these directly.
- **Scan trigger** (`pipeline.py:1187` via `enqueue_router`): fires from both ⌘K "Scan" and the empty state.

### Established Patterns
- **Fragment-only responses + single `/pipeline/stats` poll + `oob_counts` gate** (Phase 57): the record + palette are fragments; live bits are counts-only OOB off the one poll — NO new loop (D-02).
- **Counts-only OOB, never re-render the in-progress subtree** (Phase 60): applies to the open record's approval selection (D-02).
- **Server-side liveness classification** (`agent_liveness`): the k8s Active/Waiting/Idle derivation follows the same "classify server-side, inject on the row" shape (D-07).
- **`@alpinejs/focus` `x-trap.inert.noscroll`**: the one new dep — load `<script defer>` before Alpine core, version == Alpine core (3.15.12). Recompute SRI.

### Integration Points
- ⌘K wiring lands in the shell/search routers over `search_queries.search()`; the Artists distinct query is the only new read query.
- The Agents page gains a Compute-lanes section reading `CloudJob` (alongside the existing `admin_agents.py` table).
- The empty state renders in the home/Analyze workspace when file count == 0, reusing `scan_roots` + the scan trigger.
- The record fragment composes existing partials (`_diff_row.html`, track/proposal detail partials, window/timeline data) — supersede-in-place; leave the dead-template AST guard green (removal = CUT-02 / Phase 62).
</code_context>

<specifics>
## Specific Ideas

- **`prototype.html` + design §8/§9 are the canonical visual/behavioral target** for the record slide-in, ⌘K palette, Agents page, and empty/scan — match them, not a fresh interpretation.
- **The k8s lane must never read as DEAD** — the whole point of RECORD-03 / KDEPLOY-04. Idle ≠ dead; Waiting = quota-wait (inadmissible), Active = running workloads.
- **Conservative additive posture:** the only backend touches are read-only queries (distinct-artist for ⌘K, CloudJob aggregation for k8s liveness). No analysis/proposal/execution logic changes; no new write endpoints; no filesystem-listing endpoint (empty state reuses `scan_roots`).
- **One new dep, exact-version:** `@alpinejs/focus@3.15.12` matching Alpine core; load order and SRI matter (a stale hash silently blocks the script).
</specifics>

<deferred>
## Deferred Ideas

- **Empty 350px right `<aside>` removal** (`shell.html:164`) — superseded by the D-01 slide-in but physically removed in Phase 62 (CUT-02) with the other dead markup.
- **Full a11y depth for the record + palette** (keyboard-rail parity, skip-link, DAG ARIA, visible focus at parity) — Phase 62 (CUT-01). This phase ships functional keyboard nav (D-04) + `x-trap`; the audited a11y baseline is Phase 62.
- **Narrow-width / responsive rail-collapse** affecting the slide-in and palette on small viewports — Phase 62 (CUT-03).
- **Free-text path field / server-side directory browser** for the empty state — explicitly rejected for D-08 (attack surface); could return as a future requirement if scan_roots-only proves insufficient, but not scoped now.
- **Per-artist entity pages / artist as a first-class result_type** — beyond the distinct-artist filter facet (D-05); not requested.

None arose as scope creep — these are conscious later-phase boundaries or rejected alternatives.
</deferred>

---

*Phase: 61-full-record-k-agents*
*Context gathered: 2026-07-01*
